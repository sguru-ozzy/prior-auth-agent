"""Answer reviewed question meanings from supplied facts, preserving source evidence."""

import copy
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, ValidationError

from prior_auth_agent.agent.proposals import digest, normalize, stop
from prior_auth_agent.contracts import StrictModel
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.storage.jsonio import load_object
from prior_auth_agent.validation import get_fact

DEFAULT_WORKFLOW_SETTINGS = Path(str(files("prior_auth_agent.config").joinpath("workflow.json")))


class QuestionRule(StrictModel):
    labels: list[str] = Field(min_length=1)
    kinds: list[str] = Field(min_length=1)
    source: str
    format: Literal["text", "boolean", "date", "therapy_test", "therapy_detail"] = "text"


class WorkflowSettings(StrictModel):
    version: str
    auth_origins: dict[str, list[str]]
    attestation_statement: str
    submission_labels: dict[str, str]
    labels: dict[str, str]
    validation_fields: dict[str, str]
    new_patient_fields: dict[str, str]
    state_option_pattern: str
    question_rules: list[QuestionRule]
    boolean_answers: dict[str, str]
    sex_labels: dict[str, str]
    supported_systemic_therapies: list[str]
    paths: dict[str, str]
    wait_ms: int = Field(ge=100, le=20000)
    max_question_rounds: int = Field(ge=1, le=3)
    max_wizard_writes: int = Field(ge=1, le=100)

    def rule(self, wording: str, kind: str) -> QuestionRule | None:
        matches = [
            rule
            for rule in self.question_rules
            if normalize(wording) in {normalize(label) for label in rule.labels} and kind in rule.kinds
        ]
        if len(matches) > 1:
            raise stop(
                "question_ambiguous",
                "The question has multiple reviewed meanings.",
                "Review the mapping.",
                "portal.questions",
            )
        return matches[0] if matches else None


def load_workflow_settings(path: Path | None = None) -> WorkflowSettings:
    try:
        return WorkflowSettings.model_validate(
            load_object(path or DEFAULT_WORKFLOW_SETTINGS, "workflow.config", 1048576)
        )
    except ValidationError:
        raise stop(
            "workflow_config_invalid",
            "The reviewed workflow configuration is invalid.",
            "Restore the reviewed configuration.",
            "workflow.config",
        ) from None


class Condition(StrictModel):
    question: str
    answer: str


class Question(StrictModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: str
    required: bool
    choices: list[str]
    show_if: Condition | None
    hint: str = ""


class AnswerPlan(StrictModel):
    answers: dict[str, str]
    stored_answers: dict[str, str]
    fields: dict[str, JsonValue]
    questions: list[Question]
    schema_digest: str


def missing(field: str) -> None:
    raise stop(
        "answer_missing",
        "The question needs a fact that the supplied record does not provide.",
        "Supply or clarify this fact before completing the request.",
        field,
    )


def source_answer(rule: QuestionRule, facts: dict[str, JsonValue], settings: WorkflowSettings) -> tuple[str, JsonValue]:
    try:
        value = get_fact(facts, rule.source)
    except KeyError:
        missing(rule.source)
    if value is None or value == "":
        missing(rule.source)
    if rule.format == "boolean":
        if not isinstance(value, bool):
            missing(rule.source)
        return settings.boolean_answers[str(value).lower()], value
    if rule.format == "date":
        if not isinstance(value, str):
            missing(rule.source)
        try:
            return date.fromisoformat(value).strftime("%m/%d/%Y"), value
        except ValueError:
            missing(rule.source)
    if rule.format in ("therapy_test", "therapy_detail"):
        if not isinstance(value, list):
            missing(rule.source)
        if rule.format == "therapy_test":
            # Classification is limited to reviewed names; an unfamiliar treatment is not a negative answer.
            supported = {normalize(name) for name in settings.supported_systemic_therapies}
            if not value or not any(
                isinstance(item, dict) and isinstance(item.get("name"), str) and normalize(item["name"]) in supported
                for item in value
            ):
                raise stop(
                    "therapy_evidence_unsupported",
                    "No reviewed conventional systemic therapy evidence is supplied.",
                    "Clarify the patient's prior systemic therapy history.",
                    rule.source,
                )
            return settings.boolean_answers["true"], value
        pieces = []
        supported = {normalize(name) for name in settings.supported_systemic_therapies}
        for item in value:
            if (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and normalize(item["name"]) not in supported
            ):
                continue
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ("name", "started", "stopped_reason")
            ):
                missing(rule.source)
            pieces.append(item["name"] + "; started " + item["started"] + "; stopped: " + item["stopped_reason"])
        if not pieces:
            missing(rule.source)
        return "\n".join(pieces), value
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        missing(rule.source)
    return str(value), value


def plan_answers(
    raw: list[dict[str, JsonValue]], facts: dict[str, JsonValue], settings: WorkflowSettings
) -> AnswerPlan:
    try:
        questions = [Question.model_validate(item) for item in raw]
    except ValidationError:
        raise stop(
            "question_contract_unknown",
            "The portal's question contract is unsupported.",
            "Review the current questions before answering.",
            "portal.questions",
        ) from None
    by_id = {question.id: question for question in questions}
    if len(by_id) != len(questions):
        raise stop(
            "question_ambiguous",
            "The portal repeats a question identifier.",
            "Review the questions.",
            "portal.questions",
        )
    answers: dict[str, str] = {}
    stored_answers: dict[str, str] = {}
    fields: dict[str, JsonValue] = {}
    active: list[Question] = []
    visiting: set[str] = set()
    evaluated: set[str] = set()
    failures: dict[str, tuple[str, InputProblem]] = {}

    def evaluate(question: Question) -> None:
        if question.id in failures:
            raise failures[question.id][1]
        try:
            evaluate_checked(question)
        except InputProblem as error:
            if not any(failure.issue is error.issue for _, failure in failures.values()):
                failures[question.id] = (question.text, error)
            raise
        finally:
            visiting.discard(question.id)

    def evaluate_checked(question: Question) -> None:
        if question.id in evaluated:
            return
        if question.id in visiting:
            raise stop(
                "question_condition_invalid",
                "Question conditions contain a cycle.",
                "Review the questionnaire.",
                "portal.questions",
            )
        visiting.add(question.id)
        if question.show_if is not None:
            condition = question.show_if
            if condition.question not in by_id:
                raise stop(
                    "question_condition_invalid",
                    "A question condition has no referenced question.",
                    "Review the questionnaire.",
                    "portal.questions",
                )
            evaluate(by_id[condition.question])
            if answers.get(condition.question) != condition.answer:
                visiting.remove(question.id)
                evaluated.add(question.id)
                return
        rule = settings.rule(question.text, question.kind)
        if rule is None:
            if question.required:
                raise stop(
                    "question_unreviewed",
                    "A required question has no reviewed source mapping.",
                    "Review the question: " + question.text,
                    "portal.questions",
                )
            answer = ""
        else:
            try:
                answer, value = source_answer(rule, facts, settings)
            except InputProblem as error:
                # Optional missing values remain blank; semantic ambiguity and other failures still stop.
                if not question.required and error.issue.code == "answer_missing":
                    answer = ""
                else:
                    raise
            else:
                fields[rule.source] = copy.deepcopy(value)
        if answer and question.choices and answer not in question.choices:
            raise stop(
                "answer_choice_unavailable",
                "The supplied answer is not one of the portal's choices.",
                "Review the source value and the plan's allowed answers.",
                rule.source if rule else "portal.questions",
            )
        answers[question.id] = answer
        stored_answers[question.id] = (
            str(fields[rule.source]) if rule is not None and rule.format == "date" and answer else answer
        )
        active.append(question)
        visiting.remove(question.id)
        evaluated.add(question.id)

    for question in questions:
        try:
            evaluate(question)
        except InputProblem:
            pass
    if failures:
        if len(failures) == 1:
            raise next(iter(failures.values()))[1]
        details = [wording + ": " + error.issue.problem for wording, error in failures.values()]
        raise stop(
            "answers_unavailable",
            "Cannot answer the following questions: " + "; ".join(details),
            "Clinical staff must supply or clarify the listed facts and review their source mappings.",
            "portal.questions",
        )
    return AnswerPlan(
        answers=answers, stored_answers=stored_answers, fields=fields, questions=active, schema_digest=digest(raw)
    )
