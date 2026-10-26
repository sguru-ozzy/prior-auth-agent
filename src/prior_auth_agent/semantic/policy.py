"""Deterministic subject, meaning, source-value and answer-choice authorization."""

import copy
import re
from datetime import date

from pydantic import JsonValue

from prior_auth_agent.agent.proposals import digest, normalize, stop
from prior_auth_agent.agent.questions import QuestionRule, WorkflowSettings, source_answer
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.semantic.bindings import BindingResolver
from prior_auth_agent.semantic.contracts import FieldRule, SemanticSettings, UIAction, UIControl, UISnapshot, matches
from prior_auth_agent.validation import get_fact


class FieldPolicy:
    def __init__(
        self,
        facts: dict[str, JsonValue],
        settings: SemanticSettings,
        workflow: WorkflowSettings,
        resolver: BindingResolver | None = None,
    ) -> None:
        self.facts = facts
        self.settings = settings
        self.workflow = workflow
        self.resolver = resolver

    def rule(self, control: UIControl) -> FieldRule | None:
        wording = control.group if control.role == "radio" else control.label
        rules = [rule for rule in self.settings.fields if matches(wording, rule.labels)]
        if len(rules) > 1:
            raise stop(
                "field_meaning_ambiguous",
                "A field has multiple supported meanings.",
                "Review the field's subject, units and source mapping.",
                "portal.questions",
            )
        if not rules:
            if self.resolver is None:
                return None
            proposed = self.resolver.resolve(control)
            if proposed is None:
                return None
            rules = [proposed]
        rule = rules[0]
        if rule.contexts and not any(normalize(context) in normalize(control.context) for context in rule.contexts):
            raise stop(
                "field_subject_unverified",
                "The field's subject context is not supported.",
                "Clarify who the field concerns.",
                rule.source,
            )
        if rule.source.startswith("patient."):
            context = normalize(control.context + " " + control.label)
            others = self.settings.context_subjects.get("prescriber", []) + self.settings.context_subjects.get(
                "other_person", []
            )
            patient = self.settings.context_subjects.get("patient", [])
            if any(word in context for word in others) or (
                not any(word in context for word in patient)
                and rule.source in ("patient.first_name", "patient.last_name")
            ):
                raise stop(
                    "field_subject_unverified",
                    "The field cannot be confirmed to concern the patient.",
                    "Review the subject before entering a patient value.",
                    rule.source,
                )
        return rule

    def answer(self, rule: FieldRule, control: UIControl) -> tuple[str, JsonValue]:
        try:
            value = get_fact(self.facts, rule.source)
        except KeyError:
            value = None
        if value is None or value == "":
            raise stop(
                "answer_missing",
                "The supplied record does not provide this answer.",
                "Supply or clarify the missing fact.",
                rule.source,
            )
        if rule.format == "sex":
            if not isinstance(value, str) or value not in self.workflow.sex_labels:
                raise stop(
                    "answer_unsupported",
                    "The source value has no reviewed display format.",
                    "Review the source and portal options.",
                    rule.source,
                )
            return self.workflow.sex_labels[value], value
        if rule.format == "date" and control.kind == "date":
            if not isinstance(value, str):
                raise stop(
                    "answer_unsupported", "The date value is unsupported.", "Review the source date.", rule.source
                )
            return date.fromisoformat(value).isoformat(), value
        if rule.format == "boolean" and control.role == "checkbox":
            if not isinstance(value, bool):
                raise stop(
                    "answer_unsupported",
                    "A checkbox requires an explicit source boolean.",
                    "Clarify the answer.",
                    rule.source,
                )
            return "true" if value else "false", value
        question_rule = QuestionRule(
            labels=rule.labels,
            kinds=[control.kind],
            source=rule.source,
            format=rule.format if rule.format != "sex" else "text",
        )
        return source_answer(question_rule, self.facts, self.workflow)

    def bindings(self, snapshot: UISnapshot) -> list[tuple[UIControl, FieldRule, str, JsonValue]]:
        bindings = []
        seen: dict[tuple[str, str], str] = {}
        for control in snapshot.controls:
            if not control.enabled or control.role not in ("textbox", "combobox", "radio", "checkbox", "spinbutton"):
                continue
            if any(matches(control.label, labels) for labels in self.settings.search_fields.values()):
                continue
            if (control.role == "radio" and matches(control.group, self.settings.intents["form_choices"])) or (
                control.role == "combobox"
                and control.options
                and matches(control.label, self.settings.intents["form_choices"])
            ):
                continue
            if control.role == "checkbox" and any(
                re.search(pattern, control.label) for pattern in self.settings.attestation_patterns
            ):
                continue
            rule = self.rule(control)
            if rule is None:
                populated = control.checked if control.role in ("radio", "checkbox") else bool(control.value)
                if control.required or populated or control.role == "checkbox":
                    raise stop(
                        "question_unreviewed",
                        "A required or populated field has no supported source mapping.",
                        "Review this field before continuing: " + (control.group or control.label),
                        "portal.questions",
                    )
                continue
            if rule.source == "attestation_name" and not any(
                re.search(pattern, snapshot.text) for pattern in self.settings.attestation_patterns
            ):
                raise stop(
                    "attestation_meaning_unverified",
                    "This name field is not tied to an observed accuracy attestation.",
                    "Review who the name field concerns before entering the staff name.",
                    "attestation_name",
                )
            try:
                answer, value = self.answer(rule, control)
            except InputProblem as error:
                if (
                    error.issue.code == "answer_missing"
                    and not control.required
                    and not control.value
                    and not control.checked
                ):
                    continue
                raise
            key = control.group if control.role == "radio" else control.ref
            semantic_key = (rule.source, rule.format)
            if semantic_key in seen and seen[semantic_key] != key:
                raise stop(
                    "field_target_ambiguous",
                    "More than one field could receive the same source value.",
                    "Review the field mapping and subject.",
                    rule.source,
                )
            seen[semantic_key] = key
            bindings.append((control, rule, answer, copy.deepcopy(value)))
        return bindings

    def satisfied(self, control: UIControl, answer: str) -> bool:
        if control.role == "checkbox":
            return control.checked == (answer == "true")
        if control.role == "radio":
            return matches(control.label, [answer]) and control.checked
        if control.options:
            selected = [option.label for option in control.options if option.value == control.value]
            return selected == [answer]
        return control.value == answer

    def fill_actions(self, snapshot: UISnapshot) -> tuple[list[UIAction], dict[str, JsonValue]]:
        bindings = self.bindings(snapshot)
        actions = []
        fields: dict[str, JsonValue] = {}
        handled: set[str] = set()
        for control, rule, answer, value in bindings:
            if control.role == "radio":
                if control.group in handled:
                    continue
                handled.add(control.group)
                options = [c for c in snapshot.controls if c.role == "radio" and c.group == control.group and c.enabled]
                if sum(c.checked for c in options) > 1:
                    raise stop(
                        "answer_state_ambiguous",
                        "More than one answer is selected for one question.",
                        "Review the saved choices.",
                        rule.source,
                    )
                selected = [c for c in options if matches(c.label, [answer])]
                if len(selected) != 1:
                    raise stop(
                        "answer_choice_ambiguous",
                        "The source answer does not identify one available choice.",
                        "Review the question and choices.",
                        rule.source,
                    )
                control = selected[0]
            if self.satisfied(control, answer):
                fields[rule.source] = value
                continue
            if control.options:
                choices = [o for o in control.options if o.label == answer and not o.disabled]
                if len(choices) != 1:
                    raise stop(
                        "answer_choice_unavailable",
                        "The supplied answer is not one exact available choice.",
                        "Review the source and portal options.",
                        rule.source,
                    )
                method = "select"
            elif control.role in ("radio", "checkbox"):
                method = "check"
                answer = "true" if control.role == "radio" else answer
            elif control.role in ("textbox", "spinbutton") or control.kind in (
                "input",
                "text",
                "search",
                "textarea",
                "date",
                "number",
                "email",
                "tel",
            ):
                method = "fill"
            else:
                raise stop(
                    "control_unsupported",
                    "This field's widget is not supported safely.",
                    "Review the custom widget.",
                    rule.source,
                )
            actions.append(
                UIAction(intent="fill_fact", target=control.ref, source=rule.source, value=answer, method=method)
            )
        return actions, fields

    def verify(self, snapshot: UISnapshot, expected: dict[str, JsonValue]) -> None:
        actions, observed = self.fill_actions(snapshot)
        if actions or any(
            field not in observed or digest(value) != digest(observed[field]) for field, value in expected.items()
        ):
            raise stop(
                "answers_unverified",
                "Saved answers cannot be read back exactly from the source facts.",
                "Review the saved questionnaire and defaults.",
                "portal.questions",
            )
