"""Recovery results survive a changed output path and never require a repeated submission."""

import json
import time
from pathlib import Path

import pytest
from pydantic import JsonValue

from prior_auth_agent.agent.recovery import (
    History,
    RecoveryGuard,
    RequestMatch,
    load_recovery_settings,
)
from prior_auth_agent.cli import main
from prior_auth_agent.contracts import Credentials, RunResult
from prior_auth_agent.storage.audit import ActionLog

PORTAL = "http://127.0.0.1:1/dashboard"


def arguments(tmp_path: Path, record: dict[str, JsonValue], output: Path) -> list[str]:
    patient, credentials = tmp_path / "patient.json", tmp_path / "credentials.json"
    patient.write_text(json.dumps(record))
    credentials.write_text('{"username":"synthetic","password":"synthetic"}')
    return [
        "--portal-url",
        PORTAL,
        "--patient",
        str(patient),
        "--credentials",
        str(credentials),
        "--out",
        str(output),
        "--as-of",
        "2026-10-25",
        "--inspect-portal",
        "--state-dir",
        str(tmp_path / "state"),
    ]


def seed_outcome(tmp_path: Path, record: dict[str, JsonValue], result_status: str) -> RunResult:
    log = ActionLog(tmp_path / "seed.actions.jsonl")
    try:
        with RecoveryGuard(
            tmp_path / "state",
            PORTAL,
            Credentials(username="synthetic", password="synthetic"),
            record,
            load_recovery_settings(),
            log,
        ) as guard:
            guard.before_create(
                History(
                    case_key=guard.journal.case_key,
                    record_digest=guard.journal.record_digest,
                    requests=[],
                    complete=True,
                    checked_at=time.monotonic(),
                )
            )
            guard.created("REQ-1")
            guard.before_send(
                History(
                    case_key=guard.journal.case_key,
                    record_digest=guard.journal.record_digest,
                    requests=[RequestMatch(request_id="REQ-1", state="open")],
                    complete=True,
                    checked_at=time.monotonic(),
                ),
                {},
            )
            outcome = RunResult(
                status=result_status,
                reason="Verified synthetic portal outcome",
                confirmation_id="QMS-TEST" if result_status == "SUCCESS" else "",
            )
            guard.finish(outcome, request_id="REQ-1")
            return outcome
    finally:
        log.close()


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED"])
def test_cached_outcome_replays_to_different_output_without_browser_or_api_key(
    tmp_path: Path,
    record: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    expected = seed_outcome(tmp_path, record, status)
    outputs = []
    for name in ("first", "different-output"):
        output = tmp_path / (name + ".json")
        assert main(arguments(tmp_path, record, output) + ["--planner", "model"]) == 0
        outputs.append(output.read_bytes())
        assert json.loads(output.read_text()) == expected.model_dump()
        events = [
            json.loads(line)["event"] for line in output.with_name(name + ".actions.jsonl").read_text().splitlines()
        ]
        assert "outcome_replayed" in events and "page_observed" not in events and "model_requested" not in events
    assert outputs[0] == outputs[1]


def test_result_and_log_must_be_outside_the_recovery_directory(
    tmp_path: Path,
    record: dict[str, JsonValue],
) -> None:
    output = tmp_path / "state" / "preserve.json"
    output.parent.mkdir(mode=0o700)
    output.write_text("preserved")
    assert main(arguments(tmp_path, record, output)) == 2
    assert output.read_text() == "preserved"


def test_action_log_hardlink_cannot_truncate_an_unrelated_journal(tmp_path: Path) -> None:
    source = tmp_path / "journal.json"
    source.write_text("preserve pending intent")
    log = tmp_path / "result.actions.jsonl"
    log.hardlink_to(source)
    with pytest.raises(OSError):
        ActionLog(log)
    assert source.read_text() == "preserve pending intent"


def test_browser_assertion_details_are_not_exposed_in_results_or_logs(
    tmp_path: Path,
    record: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class InterruptedBrowser:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> object:
            raise AssertionError("SYNTHETIC_PRIVATE_CREDENTIAL")

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("prior_auth_agent.agent.inspection.PortalBrowser", InterruptedBrowser)
    output = tmp_path / "result.json"
    assert main(arguments(tmp_path, record, output)) == 0
    combined = output.read_text() + output.with_name("result.actions.jsonl").read_text()
    captured = capsys.readouterr()
    combined += captured.out + captured.err
    assert "SYNTHETIC_PRIVATE_CREDENTIAL" not in combined
    assert json.loads(output.read_text())["escalation"]["field"] == "portal.access"
