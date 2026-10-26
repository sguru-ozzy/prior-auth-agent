"""Reviewed semantic bindings and proposals; a proposal never contains a source value."""

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from prior_auth_agent.contracts import Clinical, Coverage, Medication, Patient, PatientRecord, Prescriber, StrictModel
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.storage.jsonio import load_object

DEFAULT_BROWSER_SETTINGS = Path(str(files("prior_auth_agent.config").joinpath("browser.json")))


def stop(code: str, problem: str, action: str, field: str = "portal.workflow") -> InputProblem:
    return InputProblem(Issue(field, code, problem, action))


class Binding(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    labels: list[str] = Field(min_length=1)
    role: Literal["textbox", "button"]
    input_types: list[str]
    sections: list[str]
    purpose: Literal["username", "password", "continue", "sign_in", "fact"]
    source: str | None

    @model_validator(mode="after")
    def check_source(self) -> "Binding":
        if (self.purpose == "fact") != (self.source is not None):
            raise ValueError("Only a fact binding has a canonical source path.")
        allowed = set(PatientRecord.model_fields)
        for prefix, model in (
            ("patient", Patient),
            ("coverage", Coverage),
            ("prescriber", Prescriber),
            ("medication", Medication),
            ("clinical", Clinical),
        ):
            allowed.update(prefix + "." + name for name in model.model_fields)
        if self.source is not None and self.source not in allowed:
            raise ValueError("A fact binding must reference a declared source field.")
        if self.purpose == "fact" and not self.sections:
            raise ValueError("Fact bindings require reviewed subject context.")
        return self


class BrowserSettings(StrictModel):
    version: str
    model: str = Field(min_length=1)
    reasoning_effort: Literal["none", "low"]
    run_seconds: int = Field(ge=1, le=300)
    wait_ms: int = Field(ge=100, le=20000)
    max_actions: int = Field(ge=1, le=80)
    max_model_calls: int = Field(ge=1, le=20)
    model_timeout_seconds: int = Field(ge=1, le=60)
    transport_retries: int = Field(ge=0, le=2)
    authentication_paths: list[str] = Field(min_length=1)
    authenticated_headings: list[str] = Field(min_length=1)
    bindings: list[Binding] = Field(min_length=1)

    @model_validator(mode="after")
    def check_registry(self) -> "BrowserSettings":
        if len({binding.id for binding in self.bindings}) != len(self.bindings):
            raise ValueError("Binding IDs must be unique.")
        if any(not path.startswith("/") or "?" in path or "#" in path for path in self.authentication_paths):
            raise ValueError("Authentication paths must be absolute paths without query strings.")
        return self


def load_browser_settings(path: Path | None = None) -> BrowserSettings:
    try:
        return BrowserSettings.model_validate(load_object(path or DEFAULT_BROWSER_SETTINGS, "browser.config", 1048576))
    except ValidationError:
        raise stop(
            "browser_config_invalid",
            "The reviewed browser configuration is invalid.",
            "Restore the evaluated browser configuration.",
            "browser.config",
        ) from None


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


class Control(StrictModel):
    ref: str
    label: str
    role: str
    input_type: str
    section: str
    enabled: bool
    has_value: bool


class Observation(StrictModel):
    fingerprint: str
    controls: list[Control]
    authenticated: bool


class Candidate(StrictModel):
    binding_id: str
    control_ref: str


class Proposal(StrictModel):
    # Every property is required, including nullable references, for strict function calling.
    action: Literal["bind", "wait", "escalate"]
    observation: str
    binding_id: str | None
    control_ref: str | None


def candidates(observation: Observation, settings: BrowserSettings) -> list[Candidate]:
    result = []
    for binding in settings.bindings:
        matches = [
            control
            for control in observation.controls
            if control.enabled
            and control.role == binding.role
            and normalize(control.label) in {normalize(label) for label in binding.labels}
            and (not binding.input_types or control.input_type in binding.input_types)
            and (not binding.sections or normalize(control.section) in {normalize(s) for s in binding.sections})
        ]
        # Ambiguity is not resolved with a model's confidence.
        if len(matches) == 1:
            result.append(Candidate(binding_id=binding.id, control_ref=matches[0].ref))
    return [
        candidate for candidate in result if sum(other.control_ref == candidate.control_ref for other in result) == 1
    ]


def validate_proposal(proposal: Proposal, observation: Observation, allowed: list[Candidate]) -> Candidate | None:
    if proposal.observation != observation.fingerprint:
        raise stop(
            "stale_proposal",
            "The proposal refers to an older page state.",
            "Inspect the current page before continuing.",
        )
    if proposal.action != "bind":
        if proposal.binding_id is not None or proposal.control_ref is not None:
            raise stop(
                "invalid_proposal",
                "A stop or wait proposal contains an action target.",
                "Review the proposal contract.",
            )
        return None
    selected = Candidate(binding_id=proposal.binding_id or "", control_ref=proposal.control_ref or "")
    if selected not in allowed:
        raise stop(
            "unreviewed_binding",
            "The proposed binding is missing, ambiguous or not reviewed.",
            "Review the field meaning and its binding before continuing.",
        )
    return selected
