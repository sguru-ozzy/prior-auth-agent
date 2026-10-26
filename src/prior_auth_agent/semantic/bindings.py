"""Evidence-checked model interpretation; supplied values remain owned by FieldPolicy."""

import re

from prior_auth_agent.agent.proposals import digest, stop
from prior_auth_agent.semantic.contracts import BindingRule, FieldRule, UIControl, UISnapshot, matches
from prior_auth_agent.semantic.planner import SemanticPlanner


class BindingResolver:
    def __init__(self, planner: SemanticPlanner) -> None:
        self.planner = planner
        self.settings = planner.policy

    def metadata(self, control: UIControl) -> dict[str, object]:
        # Never include values, hrefs, form destinations, body text or patient records.
        return {
            "label": self.planner.redact(control.label),
            "group": self.planner.redact(control.group),
            "help": self.planner.redact(control.help),
            "context": self.planner.redact(control.context),
            "role": control.role,
            "kind": control.kind,
            "choices": [self.planner.redact(o.label) for o in control.options if not o.disabled],
        }

    @staticmethod
    def _text(metadata: dict[str, object], keys: tuple[str, ...]) -> str:
        # A source marker such as [patient.first_name] must not establish a subject.
        return " ".join(re.sub(r"\[[^\]]*\]", "", str(metadata[key])) for key in keys)

    def eligible(self, rule: BindingRule, metadata: dict[str, object]) -> bool:
        meaning = self._text(metadata, ("label", "group", "help"))
        context = meaning + " " + self._text(metadata, ("context",))
        detail = metadata["role"] == "detail"
        return (
            (detail or metadata["role"] in rule.roles and metadata["kind"] in rule.kinds)
            and all(re.search(pattern, meaning) for pattern in rule.meaning_patterns)
            and any(re.search(pattern, context) for pattern in rule.subject_patterns)
            and not any(re.search(pattern, context) for pattern in rule.forbidden_patterns)
        )

    def resolve(self, control: UIControl) -> FieldRule | None:
        metadata = self.metadata(control)
        eligible = {rule.id: rule for rule in self.settings.binding_rules if self.eligible(rule, metadata)}
        if len(eligible) != 1 or self.planner.model is None:
            return None
        observation = digest(metadata)
        payload = {
            "observation": observation,
            "field": metadata,
            "catalog": [
                {"binding_id": rule.id, "description": rule.description}
                for rule in self.settings.binding_rules
                if control.role == "detail" or control.role in rule.roles and control.kind in rule.kinds
            ],
        }
        proposal = self.planner.bind(payload)
        if proposal is None:
            return None
        if proposal.observation != observation:
            raise stop(
                "stale_binding",
                "The binding does not describe the current field.",
                "Review the current field wording and context.",
                "model.proposal",
            )
        if proposal.binding_id is None:
            return None
        rule = eligible.get(proposal.binding_id)
        quoted = [str(metadata[key]) for key in ("label", "group", "help", "context")]
        evidence = " ".join(proposal.evidence)
        if (
            rule is None
            or not proposal.evidence
            or not all(re.search(pattern, evidence) for pattern in rule.meaning_patterns)
            or not any(re.search(pattern, evidence) for pattern in rule.subject_patterns)
            or any(
                not quote.strip() or "[" in quote or not any(quote in text for text in quoted)
                for quote in proposal.evidence
            )
            or not any(
                quote in str(metadata["label"]) or quote in str(metadata["group"]) for quote in proposal.evidence
            )
        ):
            raise stop(
                "binding_unverified",
                "The model binding fails meaning, subject, widget or evidence checks.",
                "Review the subject, units and source mapping for: " + str(metadata["group"] or metadata["label"]),
                next(iter(eligible.values())).field.source,
            )
        self.planner.log.event("semantic_binding_authorized", observation=observation, binding=rule.id)
        # Preserve the reviewed source/format and bind it only to this observed description.
        wording = control.group if control.role == "radio" else control.label
        return rule.field.model_copy(update={"labels": [wording]})

    @staticmethod
    def detail_control(label: str, context: str) -> UIControl:
        return UIControl(
            ref="detail",
            label=label,
            context=context,
            group="",
            help="",
            evidence="",
            region_text="",
            role="detail",
            kind="text",
            value="",
            checked=False,
            invalid=False,
            enabled=True,
            required=False,
            href="",
            form_action="",
            form_method="",
            options=[],
        )

    def label_matches(self, label: str, context: str, field: str) -> bool:
        control = self.detail_control(label, context)
        metadata = self.metadata(control)
        if not any(
            rule.field.source == field and self.eligible(rule, metadata) for rule in self.settings.binding_rules
        ):
            return False
        rule = self.resolve(control)
        return rule is not None and rule.source == field

    def values(self, snapshot: UISnapshot, field: str) -> list[str]:
        labels = self.settings.detail_labels.get(field, [])
        known_labels = [label for values in self.settings.detail_labels.values() for label in values]
        values = snapshot.values(labels)
        for pair in snapshot.pairs:
            if matches(pair.label, known_labels):
                continue
            control = self.detail_control(pair.label, pair.context)
            metadata = self.metadata(control)
            if not any(
                rule.field.source == field and self.eligible(rule, metadata) for rule in self.settings.binding_rules
            ):
                continue
            rule = self.resolve(control)
            if rule is not None and rule.source == field:
                values.append(pair.value)
        return list(dict.fromkeys(values))
