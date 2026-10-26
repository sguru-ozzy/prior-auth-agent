"""The evaluator rejects misleading outcomes and never repairs source records."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import JsonValue, ValidationError

from prior_auth_agent.contracts import RunResult
from prior_auth_agent.evaluation.contracts import (
    Case,
    Suite,
    outcome_checks,
    patient_for,
    portal_checks,
    source_fields_match,
)
from prior_auth_agent.evaluation.portal import FreshPortal

MANIFEST = Path(__file__).resolve().parents[2] / "evals" / "portal_a.json"


def suite() -> Suite:
    return Suite.model_validate_json(MANIFEST.read_text())


def request(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
    patient, coverage, medication = record["patient"], record["coverage"], record["medication"]
    assert isinstance(patient, dict) and isinstance(coverage, dict) and isinstance(medication, dict)
    return {
        "key": "REQ1",
        "member_id": patient["member_id"],
        "date_of_birth": patient["date_of_birth"],
        "drug": medication["name"],
        "drug_package": medication["package"],
        "status": "Sent to plan",
        "patient_detail": {**patient, "gender": "Female"},
        "plan_detail": coverage,
        "sends": [{"confirmation": "QMS-1", "attested_by": record["attestation_name"], "round": 1}],
        "rounds": [{"number": 1, "saved_at": 1.0, "sent_at": 2.0, "missing": []}],
    }


def success(record: dict[str, JsonValue]) -> RunResult:
    patient = record["patient"]
    assert isinstance(patient, dict)
    return RunResult(
        status="SUCCESS",
        reason="Sent",
        confirmation_id="QMS-1",
        fields_filled={"patient.member_id": patient["member_id"]},
    )


def test_case_mutations_are_explicit_and_leave_supplied_records_unchanged(record: dict[str, JsonValue]) -> None:
    original = copy.deepcopy(record)
    case = Case(
        id="bad-member",
        group="validation",
        record_id="P001",
        changes={"patient.member_id": "INVALID"},
        expected_status="ESCALATED",
    )
    changed = patient_for(case, [record])
    assert changed["patient"] != record["patient"] and record == original


def test_unknown_mutation_path_is_not_silently_created(record: dict[str, JsonValue]) -> None:
    case = Case(
        id="bad-path",
        group="validation",
        record_id="P001",
        changes={"patient.nickname": "invented"},
        expected_status="ESCALATED",
    )
    with pytest.raises(ValueError):
        patient_for(case, [record])


def test_duplicate_case_ids_are_rejected() -> None:
    data = json.loads(MANIFEST.read_text())
    data["cases"].append(data["cases"][0])
    with pytest.raises(ValidationError):
        Suite.model_validate_json(json.dumps(data))


def test_unexpected_handoff_is_not_counted_as_completion(record: dict[str, JsonValue]) -> None:
    case = next(case for case in suite().cases if case.id == "clean-p003")
    result = RunResult(
        status="ESCALATED",
        reason="Blocked",
        escalation={"field": "portal.access", "problem": "Unavailable", "suggested_action": "Review portal"},
    )
    assert outcome_checks(case, result, record)["expected_status"] is False


def test_false_and_zero_are_not_interchangeable(record: dict[str, JsonValue]) -> None:
    clinical = record["clinical"]
    assert isinstance(clinical, dict)
    clinical["body_surface_area_percent"] = 0
    result = RunResult(status="FAILED", reason="Rejected", fields_filled={"clinical.body_surface_area_percent": False})
    assert not source_fields_match(result, record)


def test_success_is_checked_against_public_identity_send_and_receipt(record: dict[str, JsonValue]) -> None:
    assert all(portal_checks(success(record), record, [request(record)], suite().oracle).values())


@pytest.mark.parametrize(
    "change",
    ["wrong-receipt", "double-send", "wrong-patient", "wrong-plan", "unsigned", "missing-answers", "unknown-status"],
)
def test_result_success_cannot_hide_bad_portal_evidence(record: dict[str, JsonValue], change: str) -> None:
    actual = request(record)
    if change == "wrong-receipt":
        actual["sends"][0]["confirmation"] = "QMS-OTHER"
    elif change == "double-send":
        actual["sends"].append(copy.deepcopy(actual["sends"][0]))
    elif change == "wrong-patient":
        actual["member_id"] = "OTHER"
    elif change == "wrong-plan":
        actual["plan_detail"] = {"bin": "OTHER"}
    elif change == "unsigned":
        actual["sends"][0]["attested_by"] = "OTHER"
    elif change == "missing-answers":
        actual["rounds"][0]["missing"] = ["required-question"]
    else:
        actual["status"] = "Unreviewed"
    assert not all(portal_checks(success(record), record, [actual], suite().oracle).values())


def test_escalation_cannot_hide_an_actual_send(record: dict[str, JsonValue]) -> None:
    result = RunResult(
        status="ESCALATED",
        reason="Blocked",
        escalation={"field": "portal.request", "problem": "Uncertain", "suggested_action": "Inspect request"},
    )
    assert not all(portal_checks(result, record, [request(record)], suite().oracle).values())


def test_fresh_server_never_kills_an_existing_listener(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock()
    monkeypatch.setattr("prior_auth_agent.evaluation.portal.occupied", lambda port: True)
    monkeypatch.setattr("prior_auth_agent.evaluation.portal.subprocess.Popen", process)
    with pytest.raises(RuntimeError, match="occupied"):
        with FreshPortal(tmp_path, Path("python"), tmp_path, 42):
            pass
    process.assert_not_called()


def test_startup_timeout_terminates_only_the_owned_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr("prior_auth_agent.evaluation.portal.occupied", lambda port: False)
    monkeypatch.setattr("prior_auth_agent.evaluation.portal.subprocess.Popen", Mock(return_value=process))
    monkeypatch.setattr("prior_auth_agent.evaluation.portal.time.monotonic", Mock(side_effect=[0, 31]))
    with pytest.raises(RuntimeError, match="30 seconds"):
        with FreshPortal(tmp_path, Path("python"), tmp_path, 42):
            pass
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=10)
    process.kill.assert_not_called()
