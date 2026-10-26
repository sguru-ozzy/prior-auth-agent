"""Run contracts, redaction, repeatability and protected artifact paths."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from prior_auth_agent.cli import main
from prior_auth_agent.contracts import Escalation, RunResult
from prior_auth_agent.runner import action_log_path
from prior_auth_agent.validation import DEFAULT_SETTINGS


def inputs(tmp_path: Path, record: dict[str, JsonValue]) -> tuple[Path, Path, Path]:
    patient = tmp_path / "patient record.json"
    credentials = tmp_path / "credentials.json"
    output = tmp_path / "artifacts" / "result.json"
    patient.write_text(json.dumps(record), encoding="utf-8")
    credentials.write_text('{"username":"synthetic-user","password":"SECRET-DO-NOT-LOG"}', encoding="utf-8")
    return patient, credentials, output


def arguments(patient: Path, credentials: Path, output: Path) -> list[str]:
    return [
        "--portal-url",
        "http://127.0.0.1:9463/dashboard",
        "--patient",
        str(patient),
        "--credentials",
        str(credentials),
        "--out",
        str(output),
        "--as-of",
        "2026-10-25",
    ]


def test_valid_record_reports_honest_handoff_without_network(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    assert main(arguments(patient, credentials, output)) == 0
    result = RunResult.model_validate_json(output.read_text())
    assert result.status == "ESCALATED"
    assert result.escalation.field == "portal.workflow"
    assert result.confirmation_id == ""
    assert result.fields_filled == {}
    assert stat.S_IMODE(action_log_path(output).stat().st_mode) == 0o600


def test_invalid_record_stops_before_credentials_are_read(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient_data = record["patient"]
    assert isinstance(patient_data, dict)
    patient_data["member_id"] = "SENSITIVE-INVALID-MEMBER"
    patient, credentials, output = inputs(tmp_path, record)
    credentials.unlink()
    assert main(arguments(patient, credentials, output)) == 0
    result = json.loads(output.read_text())
    assert result["escalation"]["field"] == "patient.member_id"
    events = [json.loads(line) for line in action_log_path(output).read_text().splitlines()]
    assert "credentials_validated" not in [event["event"] for event in events]


def test_complete_logs_and_results_repeat_identically(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    observed = []
    for _ in range(5):
        assert main(arguments(patient, credentials, output)) == 0
        observed.append((output.read_bytes(), action_log_path(output).read_bytes()))
    assert len(set(observed)) == 1
    events = [json.loads(line) for line in observed[0][1].splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert events[-1]["event"] == "outcome_selected"


@pytest.mark.parametrize("bad_credentials", [False, True])
def test_logs_and_output_exclude_identifiers_and_credentials(
    tmp_path: Path, record: dict[str, JsonValue], bad_credentials: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    patient_data = record["patient"]
    assert isinstance(patient_data, dict)
    patient_data["first_name"] = "Sensitivefirstname"
    patient_data["last_name"] = "Sensitivelastname"
    patient, credentials, output = inputs(tmp_path, record)
    if bad_credentials:
        credentials.write_text('{"username":"synthetic-user","password":["SECRET-DO-NOT-LOG"]}', encoding="utf-8")
    assert main(arguments(patient, credentials, output)) == 0
    captured = capsys.readouterr()
    text = output.read_text() + action_log_path(output).read_text() + captured.out + captured.err
    for secret in (
        "Sensitivefirstname",
        "Sensitivelastname",
        "synthetic-user",
        "SECRET-DO-NOT-LOG",
        str(patient_data["member_id"]),
    ):
        assert secret not in text


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink", "log"])
def test_artifacts_cannot_overwrite_inputs(tmp_path: Path, record: dict[str, JsonValue], alias: str) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    output.parent.mkdir()
    if alias == "same":
        output = patient
    elif alias == "symlink":
        output.symlink_to(patient)
    elif alias == "hardlink":
        os.link(patient, output)
    else:
        credentials.rename(action_log_path(output))
        credentials = action_log_path(output)
    before = (patient.read_bytes(), credentials.read_bytes())
    assert main(arguments(patient, credentials, output)) == 2
    assert (patient.read_bytes(), credentials.read_bytes()) == before


def test_log_storage_failure_escalates(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    action_log_path(output).mkdir(parents=True)
    assert main(arguments(patient, credentials, output)) == 0
    result = json.loads(output.read_text())
    assert result["escalation"]["field"] == "artifacts.action_log"


def test_unwritable_result_returns_nonzero(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    output.mkdir(parents=True)
    assert main(arguments(patient, credentials, output)) == 2


@pytest.mark.parametrize("change", ["unknown_field", "bad_pattern", "bad_range"])
def test_invalid_config_produces_handoff(tmp_path: Path, record: dict[str, JsonValue], change: str) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    settings = json.loads(DEFAULT_SETTINGS.read_text())
    if change == "unknown_field":
        settings["name_fields"] = ["unknown.input"]
    elif change == "bad_pattern":
        settings["date_pattern"] = "["
    else:
        settings["maximum_age"] = -1
    config = tmp_path / "settings.json"
    config.write_text(json.dumps(settings), encoding="utf-8")
    assert main([*arguments(patient, credentials, output), "--config", str(config)]) == 0
    assert json.loads(output.read_text())["escalation"]["field"] == "configuration"


def test_required_cli_works_from_another_directory(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    launcher = Path(__file__).resolve().parents[2] / "run_agent"
    completed = subprocess.run(
        [str(launcher), *arguments(patient, credentials, output)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == completed.stdout == ""
    assert json.loads(output.read_text())["status"] == "ESCALATED"


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED", "ESCALATED"])
def test_invalid_result_combinations_are_rejected(status: str) -> None:
    with pytest.raises(ValidationError):
        RunResult(status=status, reason="test", confirmation_id="unexpected" if status == "FAILED" else "")


def test_success_needs_receipt_and_rejection_has_no_handoff() -> None:
    assert (
        RunResult(status="SUCCESS", reason="Verified receipt", confirmation_id="SYNTHETIC-RECEIPT").status == "SUCCESS"
    )
    assert RunResult(status="FAILED", reason="Explicit portal rejection").status == "FAILED"
    with pytest.raises(ValidationError):
        RunResult(status="SUCCESS", reason="test", confirmation_id="receipt", escalation=Escalation(field="patient"))


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///tmp/test", "https://user:secret@example.test", "not a URL"]
)
def test_invalid_portal_url_escalates(tmp_path: Path, record: dict[str, JsonValue], url: str) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    args = arguments(patient, credentials, output)
    args[1] = url
    assert main(args) == 0
    assert json.loads(output.read_text())["escalation"]["field"] == "portal.url"


def test_result_storage_failure_preserves_previous_output(
    tmp_path: Path, record: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    output.parent.mkdir()
    output.write_text("previous result", encoding="utf-8")

    def reject_replace(source: Path, destination: Path) -> None:
        raise PermissionError("simulated write failure")

    monkeypatch.setattr(os, "replace", reject_replace)
    assert main(arguments(patient, credentials, output)) == 2
    assert output.read_text() == "previous result"
    assert not list(output.parent.glob(".result.json.*"))


def test_configured_missing_fact_produces_handoff(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    coverage = record["coverage"]
    assert isinstance(coverage, dict)
    del coverage["benefit"]
    patient, credentials, output = inputs(tmp_path, record)
    settings = json.loads(DEFAULT_SETTINGS.read_text())
    settings["nonblank_fields"].append("coverage.benefit")
    config = tmp_path / "settings.json"
    config.write_text(json.dumps(settings), encoding="utf-8")
    assert main([*arguments(patient, credentials, output), "--config", str(config)]) == 0
    assert json.loads(output.read_text())["escalation"]["field"] == "coverage.benefit"


def test_log_records_validation_context(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    assert main(arguments(patient, credentials, output)) == 0
    events = [json.loads(line) for line in action_log_path(output).read_text().splitlines()]
    assert events[0]["reference_date"] == "2026-10-25"
    assert len(events[1]["configuration_digest"]) == 64


def test_output_without_filename_is_a_clean_usage_failure(
    tmp_path: Path, record: dict[str, JsonValue], capsys: pytest.CaptureFixture[str]
) -> None:
    patient, credentials, output = inputs(tmp_path, record)
    assert main(arguments(patient, credentials, Path("/"))) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_policy_dictionary_order_does_not_change_handoff(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    patient_data = record["patient"]
    assert isinstance(patient_data, dict)
    patient_data["member_id"] = "invalid"
    patient_data["zip_code"] = "invalid"
    patient, credentials, output = inputs(tmp_path, record)
    settings = json.loads(DEFAULT_SETTINGS.read_text())
    config = tmp_path / "settings.json"
    config.write_text(json.dumps(settings), encoding="utf-8")
    args = [*arguments(patient, credentials, output), "--config", str(config)]
    assert main(args) == 0
    expected = (output.read_bytes(), action_log_path(output).read_bytes())
    settings["patterns"] = dict(reversed(list(settings["patterns"].items())))
    config.write_text(json.dumps(settings), encoding="utf-8")
    assert main(args) == 0
    assert (output.read_bytes(), action_log_path(output).read_bytes()) == expected
