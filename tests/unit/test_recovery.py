"""Durable write boundaries, duplicate decisions and restart behavior."""

import copy
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue

from prior_auth_agent.agent.recovery import (
    History,
    RecoveryGuard,
    RequestMatch,
    case_fingerprints,
    load_recovery_settings,
)
from prior_auth_agent.contracts import Credentials, RunResult
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.storage.audit import ActionLog
from prior_auth_agent.storage.journal import JournalStore, default_state_dir

PORTAL = "http://127.0.0.1:9463/dashboard"
CREDENTIALS = Credentials(username="synthetic-account", password="SYNTHETIC_SECRET")


def history(record: dict[str, JsonValue], *requests: RequestMatch, complete: bool = True, age: float = 0) -> History:
    key, source = case_fingerprints(PORTAL, CREDENTIALS, record, load_recovery_settings())
    return History(
        case_key=key,
        record_digest=source,
        requests=list(requests),
        complete=complete,
        checked_at=time.monotonic() - age,
    )


def guard(tmp_path: Path, record: dict[str, JsonValue], log: ActionLog) -> RecoveryGuard:
    return RecoveryGuard(tmp_path / "state", PORTAL, CREDENTIALS, record, load_recovery_settings(), log)


@pytest.fixture
def log(tmp_path: Path) -> Iterator[ActionLog]:
    stream = ActionLog(tmp_path / "actions.jsonl")
    try:
        yield stream
    finally:
        stream.close()


def draft(current: RecoveryGuard) -> None:
    current.before_create(history(current.facts))
    assert json.loads(current.store.path.read_text())["phase"] == "CREATE_PENDING"
    current.created("REQ-1")


def sending(current: RecoveryGuard) -> dict[str, JsonValue]:
    draft(current)
    fields = {"medication.name": current.facts["medication"]["name"]}
    current.before_send(history(current.facts, RequestMatch(request_id="REQ-1", state="open")), fields)
    assert json.loads(current.store.path.read_text())["phase"] == "SEND_PENDING"
    return fields


def test_case_key_ignores_start_path_query_and_mutable_facts(record: dict[str, JsonValue]) -> None:
    settings = load_recovery_settings()
    original, source = case_fingerprints(PORTAL, CREDENTIALS, record, settings)
    changed = copy.deepcopy(record)
    changed["patient"]["date_of_birth"] = "1980-01-01"
    changed["medication"]["package"] = "different package"
    other, changed_source = case_fingerprints(
        "http://127.0.0.1:9463/requests?scenario=slow",
        CREDENTIALS,
        changed,
        settings,
    )
    assert original == other
    assert source != changed_source


def test_default_state_location_does_not_follow_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = default_state_dir()
    monkeypatch.chdir(tmp_path)
    assert default_state_dir() == before


def test_open_request_blocks_create_and_keeps_new_state(
    tmp_path: Path, record: dict[str, JsonValue], log: ActionLog
) -> None:
    with guard(tmp_path, record, log) as current:
        with pytest.raises(InputProblem) as raised:
            current.before_create(history(record, RequestMatch(request_id="OTHER", state="open")))
        assert raised.value.issue.code == "existing_open_request"
        assert current.journal.phase == "NEW"


@pytest.mark.parametrize(
    "proof",
    [
        (False, 0, []),
        (True, 100, []),
        (True, -100, []),
        (True, 0, [RequestMatch(request_id="unknown", state="unknown")]),
        (True, 0, [RequestMatch(request_id="same", state="closed"), RequestMatch(request_id="same", state="closed")]),
    ],
)
def test_incomplete_stale_or_ambiguous_proof_cannot_authorize_create(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
    proof: tuple[bool, float, list[RequestMatch]],
) -> None:
    with guard(tmp_path, record, log) as current:
        with pytest.raises(InputProblem):
            current.before_create(history(record, *proof[2], complete=proof[0], age=proof[1]))
        assert current.journal.phase == "NEW"


def test_write_intents_are_durable_before_the_caller_can_act(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    with guard(tmp_path, record, log) as current:
        fields = sending(current)
        assert stat.S_IMODE(current.store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(current.store.directory.stat().st_mode) == 0o700
        current.finish(
            RunResult(status="SUCCESS", reason="Verified receipt", confirmation_id="QMS-FIXTURE", fields_filled=fields),
            request_id="REQ-1",
        )
        assert json.loads(current.store.path.read_text())["phase"] == "RESOLVED"


@pytest.mark.parametrize("phase", ["create", "draft", "send"])
def test_restart_never_repeats_an_unresolved_write(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
    phase: str,
) -> None:
    with guard(tmp_path, record, log) as current:
        if phase == "create":
            current.before_create(history(record))
        elif phase == "draft":
            draft(current)
        else:
            sending(current)
    with guard(tmp_path, record, log) as restarted:
        with pytest.raises(InputProblem) as raised:
            restarted.inspect(history(record, RequestMatch(request_id="REQ-1", state="open")))
        assert raised.value.issue.code == "pending_write"
        with pytest.raises(InputProblem):
            restarted.before_send(history(record, RequestMatch(request_id="REQ-1", state="open")), {})


def test_interrupted_send_reconciles_only_its_own_verified_receipt(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    with guard(tmp_path, record, log) as current:
        fields = sending(current)
    with guard(tmp_path, record, log) as restarted:
        result = restarted.inspect(
            history(
                record,
                RequestMatch(
                    request_id="REQ-1",
                    state="open",
                    submitted=True,
                    confirmation_id="QMS-RECOVERED",
                ),
            )
        )
        assert result is not None and result.status == "SUCCESS"
        assert result.confirmation_id == "QMS-RECOVERED"
        assert result.fields_filled == fields
    with guard(tmp_path, record, log) as repeated:
        assert repeated.cached_outcome() == result


def test_another_requests_receipt_cannot_resolve_a_pending_send(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    with guard(tmp_path, record, log) as current:
        sending(current)
    with guard(tmp_path, record, log) as restarted:
        with pytest.raises(InputProblem):
            restarted.inspect(
                history(
                    record,
                    RequestMatch(
                        request_id="OTHER",
                        state="closed",
                        submitted=True,
                        confirmation_id="QMS-OTHER",
                    ),
                )
            )
        assert restarted.journal.phase == "SEND_PENDING"


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED"])
def test_verified_outcome_replays_after_restart(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
    status: str,
) -> None:
    with guard(tmp_path, record, log) as current:
        fields = sending(current)
        result = RunResult(
            status=status,
            reason="Verified portal outcome",
            confirmation_id="QMS-FIXTURE" if status == "SUCCESS" else "",
            fields_filled=fields,
        )
        current.finish(result, request_id="REQ-1")
    with guard(tmp_path, record, log) as restarted:
        assert restarted.cached_outcome() == result


def test_changed_record_does_not_reuse_or_replace_pending_state(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    with guard(tmp_path, record, log) as current:
        sending(current)
        path = current.store.path
        before = path.read_bytes()
    changed = copy.deepcopy(record)
    changed["clinical"]["quantity_per_28_days"] = 3
    with pytest.raises(InputProblem) as raised:
        with guard(tmp_path, changed, log):
            pass
    assert raised.value.issue.code == "record_changed"
    assert path.read_bytes() == before


def test_changed_filled_value_cannot_reach_send_pending(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    with guard(tmp_path, record, log) as current:
        draft(current)
        with pytest.raises(InputProblem) as raised:
            current.before_send(
                history(record, RequestMatch(request_id="REQ-1", state="open")), {"clinical.new_start": 1}
            )
        assert raised.value.issue.code == "changed_filled_value"
        assert current.journal.phase == "DRAFT"


def test_send_intent_does_not_keep_a_mutable_reference_to_callers_fields(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    with guard(tmp_path, record, log) as current:
        fields = sending(current)
        fields["medication.name"] = "CHANGED"
        assert current.journal.fields_filled["medication.name"] != "CHANGED"


def test_journal_write_failure_blocks_permission_and_further_attempts(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic storage failure")

    with guard(tmp_path, record, log) as current:
        monkeypatch.setattr("prior_auth_agent.storage.journal.write_atomic", fail)
        with pytest.raises(InputProblem) as raised:
            current.before_create(history(record))
        assert raised.value.issue.code == "state_unavailable"
        with pytest.raises(InputProblem) as repeated:
            current.before_create(history(record))
        assert repeated.value.issue.code == "guard_unavailable"


def test_directory_sync_failure_leaves_a_conservative_pending_intent(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = os.fsync

    def fail_directory(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory sync failure")
        original(fd)

    with guard(tmp_path, record, log) as current:
        with monkeypatch.context() as patched:
            patched.setattr(os, "fsync", fail_directory)
            with pytest.raises(InputProblem):
                current.before_create(history(record))
        assert json.loads(current.store.path.read_text())["phase"] == "CREATE_PENDING"
    with guard(tmp_path, record, log) as restarted:
        with pytest.raises(InputProblem):
            restarted.inspect(history(record))


@pytest.mark.parametrize("unsafe", ["corrupt", "symlink", "hardlink", "permissions", "version", "fields"])
def test_unsafe_journal_is_preserved_and_escalated(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
    unsafe: str,
) -> None:
    with guard(tmp_path, record, log) as current:
        path = current.store.path
    if unsafe == "corrupt":
        path.write_text('{"phase":')
    elif unsafe in ("version", "fields"):
        saved = json.loads(path.read_text())
        del saved["version" if unsafe == "version" else "fields_filled"]
        path.write_text(json.dumps(saved))
    elif unsafe == "permissions":
        path.chmod(0o644)
    else:
        original = tmp_path / "original.json"
        path.rename(original)
        if unsafe == "symlink":
            path.symlink_to(original)
        else:
            os.link(original, path)
    before = path.read_bytes()
    with pytest.raises(InputProblem):
        with guard(tmp_path, record, log):
            pass
    assert path.read_bytes() == before


def test_native_lock_excludes_another_process_and_releases_after_kill(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    directory = tmp_path / "state"
    key, source = case_fingerprints(PORTAL, CREDENTIALS, record, load_recovery_settings())
    worker = Path(__file__).resolve().parents[1] / "helpers" / "hold_case_lock.py"
    process = subprocess.Popen(
        [sys.executable, str(worker), str(directory), key, source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(InputProblem) as raised:
            with JournalStore(directory, key, source):
                pass
        assert raised.value.issue.code == "case_busy"
    finally:
        process.kill()
        process.communicate(timeout=5)
    with JournalStore(directory, key, source) as restarted:
        assert restarted.load().phase == "SEND_PENDING"
    with guard(tmp_path, record, log) as restarted_guard:
        with pytest.raises(InputProblem):
            restarted_guard.inspect(history(record))


def test_new_journal_and_action_log_exclude_source_identifiers(
    tmp_path: Path,
    record: dict[str, JsonValue],
    log: ActionLog,
) -> None:
    with guard(tmp_path, record, log) as current:
        journal = current.store.path.read_text()
    combined = journal + (tmp_path / "actions.jsonl").read_text()
    assert "SYNTHETIC_SECRET" not in combined
    assert record["patient"]["member_id"] not in combined


def test_history_cannot_be_reused_for_another_case(
    tmp_path: Path, record: dict[str, JsonValue], log: ActionLog
) -> None:
    with guard(tmp_path, record, log) as current:
        proof = history(record).model_copy(update={"case_key": "f" * 64})
        with pytest.raises(InputProblem) as raised:
            current.before_create(proof)
        assert raised.value.issue.code == "history_case_mismatch"
        assert current.journal.phase == "NEW"


def test_a_different_request_cannot_resolve_the_send_intent(
    tmp_path: Path, record: dict[str, JsonValue], log: ActionLog
) -> None:
    with guard(tmp_path, record, log) as current:
        fields = sending(current)
        with pytest.raises(InputProblem):
            current.finish(
                RunResult(
                    status="SUCCESS", reason="Verified receipt", confirmation_id="QMS-OTHER", fields_filled=fields
                ),
                request_id="OTHER",
            )
        assert current.journal.phase == "SEND_PENDING"


def test_nested_false_and_zero_are_not_interchangeable(
    tmp_path: Path, record: dict[str, JsonValue], log: ActionLog
) -> None:
    with guard(tmp_path, record, log) as current:
        draft(current)
        clinical = copy.deepcopy(record["clinical"])
        assert isinstance(clinical, dict)
        clinical["new_start"] = 1 if clinical["new_start"] else 0
        with pytest.raises(InputProblem):
            current.before_send(history(record, RequestMatch(request_id="REQ-1", state="open")), {"clinical": clinical})
        assert current.journal.phase == "DRAFT"


@pytest.mark.parametrize("phase", ["NEW", "CREATE_PENDING", "SEND_PENDING"])
def test_explicit_resume_cannot_bypass_a_pending_write(
    tmp_path: Path, record: dict[str, JsonValue], log: ActionLog, phase: str
) -> None:
    with guard(tmp_path, record, log) as current:
        if phase == "CREATE_PENDING":
            current.before_create(history(record))
        elif phase == "SEND_PENDING":
            sending(current)
        with pytest.raises(InputProblem) as raised:
            current.authorize_draft_resume(history(record, RequestMatch(request_id="REQ-1", state="open")), "REQ-1")
        assert raised.value.issue.code == "resume_not_authorized"
        assert current.journal.phase == phase


@pytest.mark.parametrize("problem", ["different-id", "other-open", "stale", "receipt"])
def test_explicit_resume_requires_fresh_unique_unsent_draft_evidence(
    tmp_path: Path, record: dict[str, JsonValue], log: ActionLog, problem: str
) -> None:
    with guard(tmp_path, record, log) as current:
        draft(current)
    matches = [
        RequestMatch(request_id="REQ-1", state="open", confirmation_id="QMS-EXISTS" if problem == "receipt" else None)
    ]
    if problem == "other-open":
        matches.append(RequestMatch(request_id="OTHER", state="open"))
    with guard(tmp_path, record, log) as restarted:
        with pytest.raises(InputProblem):
            restarted.authorize_draft_resume(
                history(record, *matches, age=100 if problem == "stale" else 0),
                "OTHER" if problem == "different-id" else "REQ-1",
            )
        assert restarted.journal.phase == "DRAFT" and not restarted.resume_authorized
