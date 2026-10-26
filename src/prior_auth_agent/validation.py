"""Apply the supplied contract and reviewed configuration without repairing facts."""

import re
from datetime import date
from importlib.resources import files
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, ValidationError

from prior_auth_agent.contracts import (
    Clinical,
    Coverage,
    Credentials,
    Medication,
    Patient,
    PatientRecord,
    Prescriber,
    PriorTherapy,
    StrictModel,
)
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.storage.jsonio import load_object


class PatternRule(StrictModel):
    pattern: str
    problem: str


class RangeRule(StrictModel):
    minimum: int
    maximum: int


class RequiredWhen(StrictModel):
    when_field: str
    equals: bool
    required_field: str
    problem: str


class Settings(StrictModel):
    version: str
    max_input_bytes: int = Field(gt=0)
    name_punctuation: str
    name_fields: list[str]
    nonblank_fields: list[str]
    patterns: dict[str, PatternRule]
    choices: dict[str, list[str]]
    ranges: dict[str, RangeRule]
    date_fields: list[str]
    date_pattern: str
    minimum_age: int = Field(ge=0)
    maximum_age: int = Field(ge=0)
    required_when: list[RequiredWhen]


DEFAULT_SETTINGS = Path(str(files("prior_auth_agent.config").joinpath("validation.json")))
# All labels come from the declared schema, never from submitted values.
_SCHEMA_LABELS = set().union(
    *(
        model.model_fields
        for model in (PatientRecord, Patient, Coverage, Prescriber, Medication, Clinical, PriorTherapy, Credentials)
    )
)


def load_settings(path: Path | None) -> Settings:
    try:
        settings = Settings.model_validate(load_object(path or DEFAULT_SETTINGS, "configuration", 1048576))
        re.compile(settings.date_pattern)
        for rule in settings.patterns.values():
            re.compile(rule.pattern)
        allowed_fields = set(PatientRecord.model_fields)
        for prefix, model in (
            ("patient", Patient),
            ("coverage", Coverage),
            ("prescriber", Prescriber),
            ("medication", Medication),
            ("clinical", Clinical),
        ):
            allowed_fields.update(prefix + "." + name for name in model.model_fields)
        configured_fields = (
            set(settings.name_fields)
            | set(settings.nonblank_fields)
            | set(settings.patterns)
            | set(settings.choices)
            | set(settings.ranges)
            | set(settings.date_fields)
        )
        for condition in settings.required_when:
            configured_fields.update((condition.when_field, condition.required_field))
        if not configured_fields <= allowed_fields or "patient.date_of_birth" not in settings.date_fields:
            raise ValueError("Configuration references unsupported fields.")
        if settings.minimum_age > settings.maximum_age:
            raise ValueError("Invalid age range.")
        if any(rule.minimum > rule.maximum for rule in settings.ranges.values()):
            raise ValueError("Invalid numeric range.")
        return settings
    except (ValidationError, ValueError, re.error):
        raise InputProblem(
            Issue(
                "configuration",
                "invalid_configuration",
                "Validation configuration is invalid.",
                "Restore the reviewed configuration before running the agent.",
            )
        ) from None


def get_fact(record: dict[str, JsonValue], path: str) -> JsonValue:
    value: JsonValue = record
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def _reject(field: str, code: str, problem: str) -> NoReturn:
    raise InputProblem(Issue(field, code, problem, "Correct this field in the source record, then rerun."))


def _required_fact(record: dict[str, JsonValue], path: str) -> JsonValue:
    try:
        return get_fact(record, path)
    except KeyError:
        _reject(path, "missing_field", "A fact required by the reviewed configuration is missing.")


def _schema_issue(error: ValidationError, prefix: str = "") -> InputProblem:
    detail = error.errors(include_input=False, include_context=False, include_url=False)[0]
    labels = []
    for part in detail["loc"]:
        if isinstance(part, int):
            labels.append(str(part))
        elif part in _SCHEMA_LABELS:
            labels.append(part)
        else:
            break
    field = ".".join(filter(None, [prefix, ".".join(labels)])) or "patient"
    if detail["type"] == "missing":
        problem, code = "Required field is missing.", "missing_field"
    else:
        problem, code = "Value has the wrong type or structure.", "invalid_type"
    return InputProblem(Issue(field, code, problem, "Provide this field using the shared data contract."))


def validate_patient(record: dict[str, JsonValue], settings: Settings, reference_date: date) -> None:
    try:
        PatientRecord.model_validate(record)
    except ValidationError as error:
        raise _schema_issue(error) from None

    for field in settings.name_fields:
        value = _required_fact(record, field)
        if not isinstance(value, str) or not any(char.isalpha() for char in value):
            _reject(field, "invalid_name", "A name containing letters is required.")
        if any(not char.isalpha() and char not in settings.name_punctuation for char in value):
            _reject(field, "invalid_name", "Names may contain only letters, hyphens, apostrophes and spaces.")

    for field in settings.nonblank_fields:
        value = _required_fact(record, field)
        if not isinstance(value, str) or not value.strip():
            _reject(field, "blank_field", "A nonblank value is required.")

    for field, rule in sorted(settings.patterns.items()):
        value = _required_fact(record, field)
        if not isinstance(value, str) or re.fullmatch(rule.pattern, value) is None:
            _reject(field, "invalid_format", rule.problem)

    for field, allowed in sorted(settings.choices.items()):
        try:
            value = get_fact(record, field)
        except KeyError:
            continue
        if value is not None and value not in allowed:
            _reject(field, "invalid_choice", "Expected one of: " + ", ".join(allowed) + ".")

    for field, bounds in sorted(settings.ranges.items()):
        try:
            value = get_fact(record, field)
        except KeyError:
            continue
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not bounds.minimum <= value <= bounds.maximum
        ):
            _reject(field, "out_of_range", f"Expected a number between {bounds.minimum} and {bounds.maximum}.")

    for condition in settings.required_when:
        if _required_fact(record, condition.when_field) is condition.equals:
            try:
                value = get_fact(record, condition.required_field)
            except KeyError:
                value = None
            if value is None:
                _reject(
                    condition.required_field,
                    "conditionally_required",
                    condition.problem,
                )

    parsed_dates: dict[str, date] = {}
    for field in settings.date_fields:
        try:
            value = get_fact(record, field)
        except KeyError:
            continue
        if value is None:
            continue
        try:
            if not isinstance(value, str) or re.fullmatch(settings.date_pattern, value) is None:
                raise ValueError
            parsed_dates[field] = date.fromisoformat(value)
        except ValueError:
            _reject(field, "invalid_date", "Expected a valid date in YYYY-MM-DD format.")

    born = parsed_dates["patient.date_of_birth"]
    age = reference_date.year - born.year - ((reference_date.month, reference_date.day) < (born.month, born.day))
    if born > reference_date or not settings.minimum_age <= age <= settings.maximum_age:
        _reject(
            "patient.date_of_birth",
            "invalid_age",
            f"Birth date must not be in the future; age must be {settings.minimum_age} to {settings.maximum_age}.",
        )


def validate_credentials(document: dict[str, JsonValue]) -> Credentials:
    try:
        credentials = Credentials.model_validate(document)
    except ValidationError as error:
        raise _schema_issue(error, "credentials") from None
    for field in ("username", "password"):
        if not getattr(credentials, field).get_secret_value():
            _reject("credentials." + field, "empty_credential", "A nonempty credential is required.")
    return credentials


def validate_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise InputProblem(
            Issue(
                "portal.url",
                "invalid_url",
                "Expected an absolute HTTP(S) URL without credentials.",
                "Provide the supplied portal start URL; keep credentials in their JSON file.",
            )
        ) from None
