"""Reviewed meanings, observed UI evidence and value-free action proposals."""

import re
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from prior_auth_agent.agent.proposals import digest, normalize, stop
from prior_auth_agent.contracts import Clinical, Coverage, Medication, Patient, PatientRecord, Prescriber, StrictModel
from prior_auth_agent.storage.jsonio import load_object

DEFAULT_SEMANTIC_SETTINGS = Path(str(files("prior_auth_agent.config").joinpath("semantic.json")))


class FieldRule(StrictModel):
    source: str
    labels: list[str] = Field(min_length=1)
    contexts: list[str] = Field(default_factory=list)
    format: Literal["text", "boolean", "date", "sex", "therapy_test", "therapy_detail"] = "text"

    @model_validator(mode="after")
    def declared_source(self) -> "FieldRule":
        allowed = set(PatientRecord.model_fields)
        for prefix, model in (
            ("patient", Patient),
            ("coverage", Coverage),
            ("medication", Medication),
            ("prescriber", Prescriber),
            ("clinical", Clinical),
        ):
            allowed.update(prefix + "." + field for field in model.model_fields)
        if self.source not in allowed or "." not in self.source and self.source not in ("pharmacy", "attestation_name"):
            raise ValueError("Semantic bindings must reference a declared canonical field.")
        return self


class SemanticSettings(StrictModel):
    version: str
    run_seconds: int = Field(ge=30, le=600)
    max_actions: int = Field(ge=1, le=200)
    max_model_calls: int = Field(ge=1, le=80)
    settle_ms: int = Field(ge=50, le=1000)
    max_unchanged: int = Field(ge=1, le=5)
    max_controls: int = Field(ge=20, le=1000)
    max_text_length: int = Field(ge=1000, le=100000)
    max_history_pages: int = Field(ge=1, le=30)
    max_history_rows: int = Field(ge=1, le=1000)
    intents: dict[str, list[str]]
    fields: list[FieldRule]
    search_fields: dict[str, list[str]]
    history_columns: dict[str, list[str]]
    detail_labels: dict[str, list[str]]
    pagination_patterns: list[str]
    empty_history: list[str]
    empty_search: list[str]
    clear_filter_values: list[str]
    non_filter_labels: list[str]
    all_history_labels: list[str]
    request_id_patterns: list[str]
    blocking_alert_patterns: list[str]
    open_statuses: list[str]
    closed_statuses: list[str]
    submitted_statuses: list[str]
    rejected_statuses: list[str]
    attestation_patterns: list[str]
    reference_patterns: list[str]
    context_subjects: dict[str, list[str]]
    required_review_fields: list[str]

    @model_validator(mode="after")
    def check_rules(self) -> "SemanticSettings":
        required = {
            "username",
            "password",
            "login",
            "history",
            "clear_filters",
            "next_page",
            "new_request",
            "new_patient",
            "patient_search",
            "medication_search",
            "find_forms",
            "create",
            "questions",
            "review",
            "advance",
            "save",
            "send",
            "dismiss",
            "form_choices",
        }
        if not required <= self.intents.keys() or not all(self.intents[key] for key in required):
            raise ValueError("Required workflow intents are missing.")
        if set(self.history_columns) != {"request_id", "medication.name", "status"}:
            raise ValueError("History column meanings are incomplete.")
        if (
            not set(self.required_review_fields + ["request_id", "confirmation_id", "status", "attestation_name"])
            <= self.detail_labels.keys()
        ):
            raise ValueError("Required detail evidence labels are missing.")
        for pattern in (
            self.attestation_patterns
            + self.reference_patterns
            + self.pagination_patterns
            + self.request_id_patterns
            + self.blocking_alert_patterns
        ):
            re.compile(pattern)
        if any(not {"first", "last", "total"} <= re.compile(p).groupindex.keys() for p in self.pagination_patterns):
            raise ValueError("History summaries must expose complete pagination bounds.")
        if {normalize(s) for s in self.open_statuses} & {normalize(s) for s in self.closed_statuses}:
            raise ValueError("History status categories overlap.")
        return self


def load_semantic_settings(path: Path | None = None) -> SemanticSettings:
    try:
        return SemanticSettings.model_validate(
            load_object(path or DEFAULT_SEMANTIC_SETTINGS, "semantic.config", 1048576)
        )
    except (ValidationError, re.error):
        raise stop(
            "semantic_config_invalid",
            "The semantic workflow configuration is invalid.",
            "Restore the reviewed semantic configuration.",
            "semantic.config",
        ) from None


def meaning(text: str) -> str:
    # Presentation markers only; units, negations and subject words remain significant.
    return normalize(re.sub(r"\s*(?:\*|\(required\)|\(optional\))\s*$", "", text, flags=re.I)).rstrip(": ?")


def matches(text: str, labels: list[str]) -> bool:
    return meaning(text) in {meaning(label) for label in labels}


class UIOption(StrictModel):
    label: str
    value: str
    disabled: bool


class UIControl(StrictModel):
    ref: str
    label: str
    group: str
    evidence: str
    region_text: str
    context: str
    role: str
    kind: str
    value: str
    checked: bool
    invalid: bool
    enabled: bool
    required: bool
    href: str
    form_action: str
    form_method: str
    options: list[UIOption]


class UIPair(StrictModel):
    label: str
    value: str


class UIRow(StrictModel):
    pairs: list[UIPair] = Field(default_factory=list)
    cells: list[str]
    controls: list[str]
    text: str


class UITable(StrictModel):
    headers: list[str]
    rows: list[UIRow]


class UISnapshot(StrictModel):
    url: str
    title: str
    text: str
    alerts: list[str]
    controls: list[UIControl]
    pairs: list[UIPair]
    tables: list[UITable]
    busy: bool
    truncated: bool

    @property
    def fingerprint(self) -> str:
        return digest(self.model_dump())

    def control(self, ref: str) -> UIControl:
        result = [c for c in self.controls if c.ref == ref]
        if len(result) != 1:
            raise stop("target_missing", "The observed control is no longer unique.", "Review the current screen.")
        return result[0]

    def values(self, labels: list[str]) -> list[str]:
        return list(dict.fromkeys(pair.value for pair in self.pairs if matches(pair.label, labels)))


class UIAction(StrictModel):
    intent: str
    target: str
    source: str | None = None
    value: str | None = None
    method: Literal["click", "fill", "select", "check"] = "click"

    @property
    def id(self) -> str:
        # The value never goes into prompts, logs or cached proposals.
        return digest(self.model_dump())


class UIProposal(StrictModel):
    observation: str
    candidate_id: str | None
