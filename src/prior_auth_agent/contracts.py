"""Typed input and output contracts; source facts remain unchanged."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, model_validator

from prior_auth_agent.errors import Issue


class SourceModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False, hide_input_in_errors=True)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)


class Patient(SourceModel):
    first_name: str
    last_name: str
    date_of_birth: str
    sex: str
    member_id: str
    state: str
    zip_code: str


class Coverage(SourceModel):
    bin: str
    pcn: str
    group: str
    plan_name: str | None = None
    benefit: str | None = None
    subscriber_employee_id: str | None = None


class Prescriber(SourceModel):
    npi: str
    name: str | None = None
    fax: str | None = None


class Medication(SourceModel):
    name: str
    package: str


class PriorTherapy(SourceModel):
    name: str = Field(min_length=1)
    started: str | None = None
    stopped_reason: str | None = None


class Clinical(SourceModel):
    diagnosis_icd10: str
    diagnosis_description: str | None = None
    body_surface_area_percent: int | float | None = None
    sensitive_areas_affected: bool
    new_start: bool
    current_therapy_start_date: str | None = None
    prior_therapies: list[PriorTherapy]
    reason_for_change: str | None = None
    notes: str | None = None
    quantity_per_28_days: int
    site_of_care: str | None = None


class PatientRecord(SourceModel):
    patient: Patient
    coverage: Coverage
    prescriber: Prescriber
    medication: Medication
    clinical: Clinical
    pharmacy: str | None = None
    attestation_name: str


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True)


class Credentials(StrictModel):
    username: SecretStr
    password: SecretStr


class Escalation(StrictModel):
    field: str = ""
    problem: str = ""
    suggested_action: str = ""


class RunResult(StrictModel):
    status: Literal["SUCCESS", "FAILED", "ESCALATED"]
    reason: str = Field(min_length=1)
    confirmation_id: str = ""
    fields_filled: dict[str, JsonValue] = Field(default_factory=dict)
    escalation: Escalation = Field(default_factory=Escalation)

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if not self.reason.strip():
            raise ValueError("A nonblank reason is required.")
        handoff = self.escalation.model_dump().values()
        if self.status == "ESCALATED":
            if self.confirmation_id or not all(value.strip() for value in handoff):
                raise ValueError("An escalation requires a complete handoff and no confirmation ID.")
        elif any(handoff):
            raise ValueError("A completed outcome must not contain escalation details.")
        if self.status == "SUCCESS" and not self.confirmation_id.strip():
            raise ValueError("Success requires a verified confirmation ID.")
        if self.status == "FAILED" and self.confirmation_id:
            raise ValueError("A rejected submission must not have a sent confirmation ID.")
        return self

    @classmethod
    def from_issue(cls, issue: Issue) -> Self:
        return cls(
            status="ESCALATED",
            reason=f"{issue.field}: {issue.problem}",
            escalation=Escalation(
                field=issue.field,
                problem=issue.problem,
                suggested_action=issue.suggested_action,
            ),
        )
