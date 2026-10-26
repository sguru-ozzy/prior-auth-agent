"""Exercise rules, strict types, missing facts and deterministic handoffs."""

from copy import deepcopy
from datetime import date

import pytest
from pydantic import JsonValue

from prior_auth_agent.errors import InputProblem
from prior_auth_agent.validation import Settings, get_fact, validate_patient

REFERENCE_DATE = date(2026, 10, 25)


def set_fact(record: dict[str, JsonValue], path: str, value: JsonValue) -> None:
    parts = path.split(".")
    target = record
    for part in parts[:-1]:
        nested = target[part]
        assert isinstance(nested, dict)
        target = nested
    target[parts[-1]] = value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("patient.first_name", "Invalid7"),
        ("patient.last_name", ""),
        ("patient.first_name", "---"),
        ("patient.date_of_birth", "2027-01-01"),
        ("patient.date_of_birth", "1905-10-25"),
        ("patient.date_of_birth", "2023-02-29"),
        ("patient.date_of_birth", "19651120"),
        ("patient.sex", "U"),
        ("patient.member_id", "ZZ30001"),
        ("patient.member_id", "ZZ3１２３４５"),
        ("patient.state", "Ohio"),
        ("patient.zip_code", "0011"),
        ("patient.zip_code", 11),
        ("coverage.bin", ""),
        ("coverage.pcn", None),
        ("coverage.group", False),
        ("prescriber.npi", "123456789"),
        ("prescriber.npi", 1999999902),
        ("medication.name", ""),
        ("medication.package", " "),
        ("clinical.diagnosis_icd10", "not a code"),
        ("clinical.body_surface_area_percent", -1),
        ("clinical.body_surface_area_percent", 101),
        ("clinical.body_surface_area_percent", False),
        ("clinical.body_surface_area_percent", "6"),
        ("clinical.sensitive_areas_affected", 0),
        ("clinical.new_start", "true"),
        ("clinical.prior_therapies", {}),
        ("clinical.quantity_per_28_days", 0),
        ("clinical.quantity_per_28_days", 13),
        ("clinical.quantity_per_28_days", True),
        ("clinical.quantity_per_28_days", 2.0),
        ("clinical.quantity_per_28_days", "2"),
        ("clinical.site_of_care", "Hospital"),
        ("clinical.current_therapy_start_date", "2022-99-99"),
        ("attestation_name", " "),
    ],
)
def test_invalid_field_is_precisely_identified(
    record: dict[str, JsonValue], settings: Settings, field: str, value: JsonValue
) -> None:
    set_fact(record, field, value)
    original = deepcopy(record)
    with pytest.raises(InputProblem) as raised:
        validate_patient(record, settings, REFERENCE_DATE)
    assert raised.value.issue.field == field
    assert raised.value.issue.suggested_action
    assert record == original


@pytest.mark.parametrize(
    "field",
    [
        "first_name",
        "last_name",
        "date_of_birth",
        "sex",
        "member_id",
        "state",
        "zip_code",
    ],
)
def test_missing_required_patient_field(record: dict[str, JsonValue], settings: Settings, field: str) -> None:
    patient = record["patient"]
    assert isinstance(patient, dict)
    del patient[field]
    with pytest.raises(InputProblem) as raised:
        validate_patient(record, settings, REFERENCE_DATE)
    assert raised.value.issue.field == "patient." + field


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("patient.first_name", "Éva-Marie"),
        ("patient.last_name", "O'Neil"),
        ("patient.date_of_birth", "2026-10-25"),
        ("patient.date_of_birth", "1905-10-26"),
        ("patient.date_of_birth", "2024-02-29"),
        ("patient.sex", "X"),
        ("patient.zip_code", "00000"),
        ("patient.state", "zz"),
        ("clinical.body_surface_area_percent", 0),
        ("clinical.body_surface_area_percent", 100),
        ("clinical.body_surface_area_percent", 0.5),
        ("clinical.body_surface_area_percent", None),
        ("clinical.sensitive_areas_affected", False),
        ("clinical.quantity_per_28_days", 1),
        ("clinical.quantity_per_28_days", 12),
        ("clinical.site_of_care", None),
        ("coverage.benefit", None),
    ],
)
def test_valid_boundaries_preserve_source(
    record: dict[str, JsonValue], settings: Settings, field: str, value: JsonValue
) -> None:
    set_fact(record, field, value)
    original = deepcopy(record)
    validate_patient(record, settings, REFERENCE_DATE)
    assert record == original
    assert type(get_fact(record, field)) is type(value)


@pytest.mark.parametrize("missing", [False, True])
def test_existing_therapy_requires_start_date(record: dict[str, JsonValue], settings: Settings, missing: bool) -> None:
    clinical = record["clinical"]
    assert isinstance(clinical, dict)
    clinical["new_start"] = False
    if missing:
        del clinical["current_therapy_start_date"]
    else:
        clinical["current_therapy_start_date"] = None
    with pytest.raises(InputProblem) as raised:
        validate_patient(record, settings, REFERENCE_DATE)
    assert raised.value.issue.field == "clinical.current_therapy_start_date"
    assert raised.value.issue.code == "conditionally_required"


def test_optional_missing_null_false_and_zero_remain_distinct(record: dict[str, JsonValue], settings: Settings) -> None:
    clinical = record["clinical"]
    coverage = record["coverage"]
    assert isinstance(clinical, dict) and isinstance(coverage, dict)
    del coverage["benefit"]
    clinical["body_surface_area_percent"] = 0
    clinical["sensitive_areas_affected"] = False
    clinical["site_of_care"] = None
    original = deepcopy(record)
    validate_patient(record, settings, REFERENCE_DATE)
    assert record == original
    with pytest.raises(KeyError):
        get_fact(record, "coverage.benefit")
    assert get_fact(record, "clinical.site_of_care") is None
    assert get_fact(record, "clinical.sensitive_areas_affected") is False
    assert type(get_fact(record, "clinical.body_surface_area_percent")) is int


def test_plan_and_product_matches_are_deferred_to_portal(record: dict[str, JsonValue], settings: Settings) -> None:
    set_fact(record, "coverage.bin", "unseen plan")
    set_fact(record, "medication.name", "unseen product")
    set_fact(record, "prescriber.npi", "0000000000")
    validate_patient(record, settings, REFERENCE_DATE)


def test_extra_source_facts_are_preserved(record: dict[str, JsonValue], settings: Settings) -> None:
    record["additional_fact"] = {"missing_value": None, "false_value": False, "zero_value": 0}
    original = deepcopy(record)
    validate_patient(record, settings, REFERENCE_DATE)
    assert record == original
