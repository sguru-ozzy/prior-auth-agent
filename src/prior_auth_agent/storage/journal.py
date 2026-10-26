"""Private, crash-durable case journals protected by a native process lock."""

import json
import os
import re
import stat
from pathlib import Path
from typing import Literal, Self

from filelock import Timeout, UnixFileLock
from pydantic import Field, JsonValue, ValidationError, model_validator

from prior_auth_agent.contracts import RunResult, StrictModel
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.storage.jsonio import load_object, write_atomic


def state_problem(code: str, problem: str) -> InputProblem:
    return InputProblem(
        Issue(
            "recovery.state",
            code,
            problem,
            "Preserve the journal and inspect the portal before continuing; do not retry a pending write.",
        )
    )


class Journal(StrictModel):
    version: Literal[1]
    case_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: Literal["NEW", "CREATE_PENDING", "DRAFT", "SEND_PENDING", "RESOLVED"]
    request_id: str | None
    fields_filled: dict[str, JsonValue]
    outcome: RunResult | None

    @model_validator(mode="after")
    def check_phase(self) -> Self:
        if self.phase in ("NEW", "CREATE_PENDING"):
            if self.request_id is not None or self.fields_filled or self.outcome is not None:
                raise ValueError("A request does not exist before creation is verified.")
        elif not self.request_id or not self.request_id.strip():
            raise ValueError("A verified request ID is required.")
        if self.phase == "RESOLVED":
            if self.outcome is None or self.outcome.status == "ESCALATED":
                raise ValueError("Resolution needs a verified terminal outcome.")
            if json.dumps(self.outcome.fields_filled, sort_keys=True) != json.dumps(self.fields_filled, sort_keys=True):
                raise ValueError("The resolved fields must match the send intent.")
        elif self.outcome is not None:
            raise ValueError("An unresolved journal cannot have a terminal outcome.")
        return self


def default_state_dir() -> Path:
    fallback = Path.home() / ".local" / "state"
    base = Path(os.environ.get("XDG_STATE_HOME") or str(fallback))
    if not base.is_absolute():
        base = fallback
    return base / "prior-auth-agent"


class JournalStore:
    def __init__(self, directory: Path, case_key: str, record_digest: str, max_bytes: int = 1048576) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", case_key) is None:
            raise state_problem("invalid_case_key", "The case key is invalid.")
        self.directory = directory.expanduser().resolve()
        self.path = self.directory / (case_key + ".json")
        self.lock_path = self.directory / (case_key + ".lock")
        self.case_key = case_key
        self.record_digest = record_digest
        self.max_bytes = max_bytes
        self.lock: UnixFileLock | None = None

    @staticmethod
    def _private_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise state_problem("unsafe_journal", "A state file is not a private, owned regular file.")

    def __enter__(self) -> "JournalStore":
        try:
            self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            metadata = self.directory.stat()
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise state_problem("unsafe_state_directory", "The state directory must be private to its owner.")
            self._private_file(self.lock_path)
            self.lock = UnixFileLock(
                self.lock_path,
                timeout=0,
                mode=0o600,
                preserve_lock_file=True,
                fallback_to_soft=False,
            )
            self.lock.acquire()
            return self
        except Timeout:
            raise InputProblem(
                Issue(
                    "recovery.case",
                    "case_busy",
                    "Another local runner owns this patient and medication case.",
                    "Wait for that run to finish, then inspect its outcome before rerunning.",
                )
            ) from None
        except (OSError, NotImplementedError):
            raise state_problem("state_unavailable", "The case lock or state directory is unavailable.") from None

    def __exit__(self, *args: object) -> None:
        if self.lock is not None and self.lock.is_locked:
            try:
                self.lock.release()
            except OSError:
                raise state_problem("state_unavailable", "The case lock could not be released safely.") from None

    def load(self) -> Journal:
        try:
            self._private_file(self.path)
            if not self.path.exists():
                journal = Journal(
                    version=1,
                    case_key=self.case_key,
                    record_digest=self.record_digest,
                    phase="NEW",
                    request_id=None,
                    fields_filled={},
                    outcome=None,
                )
                self.save(journal)
                return journal
            try:
                document = load_object(self.path, "recovery.state", self.max_bytes)
            except InputProblem:
                raise state_problem("journal_invalid", "The saved case journal is invalid or unreadable.") from None
            journal = Journal.model_validate(document)
            if journal.case_key != self.case_key:
                raise state_problem("journal_mismatch", "The journal belongs to a different case.")
            if journal.record_digest != self.record_digest:
                if journal.phase != "NEW":
                    raise state_problem("record_changed", "Source facts changed after a request write was authorized.")
                journal = journal.model_copy(update={"record_digest": self.record_digest})
                self.save(journal)
            return journal
        except ValidationError:
            raise state_problem("journal_invalid", "The saved case journal is invalid.") from None
        except OSError:
            raise state_problem("state_unavailable", "The saved case journal could not be read.") from None

    def save(self, journal: Journal) -> None:
        if self.lock is None or not self.lock.is_locked:
            raise state_problem("lock_required", "A case journal cannot change without its process lock.")
        if journal.case_key != self.case_key or journal.record_digest != self.record_digest:
            raise state_problem("journal_mismatch", "The journal does not match this case and source record.")
        try:
            journal = Journal.model_validate(journal.model_dump())
            self._private_file(self.path)
            write_atomic(self.path, journal.model_dump_json(indent=2) + "\n", durable=True)
        except OSError:
            raise state_problem(
                "state_unavailable", "The journal could not be persisted durably; no write is authorized."
            ) from None
