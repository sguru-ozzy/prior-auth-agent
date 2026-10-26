"""Duplicate decisions, durable write boundaries and interrupted-send reconciliation."""

import copy
import re
import time
from importlib.resources import files
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, ValidationError, model_validator

from prior_auth_agent.agent.proposals import digest, normalize
from prior_auth_agent.contracts import Credentials, RunResult, StrictModel
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.storage.audit import ActionLog
from prior_auth_agent.storage.journal import JournalStore, state_problem
from prior_auth_agent.storage.jsonio import load_object
from prior_auth_agent.validation import get_fact, validate_url

DEFAULT_RECOVERY_SETTINGS = Path(str(files("prior_auth_agent.config").joinpath("recovery.json")))


class HistoryRules(StrictModel):
    navigation: str
    heading: str
    clear_filters: str
    filter_labels: list[str]
    columns: dict[str, str]
    summary_pattern: str
    next_button: str
    max_pages: int = Field(ge=1, le=20)
    max_requests: int = Field(ge=1, le=1000)
    detail_fields: dict[str, str]
    open_statuses: list[str]
    closed_statuses: list[str]
    submitted_statuses: list[str]
    receipt_pattern: str


class RecoverySettings(StrictModel):
    version: str
    identity_fields: list[str] = Field(min_length=1)
    evidence_max_age_seconds: int = Field(ge=1, le=60)
    max_journal_bytes: int = Field(ge=1024, le=1048576)
    history: HistoryRules

    @model_validator(mode="after")
    def check_rules(self) -> "RecoverySettings":
        if set(self.history.columns) != {"key", "drug", "status"}:
            raise ValueError("Request history columns are incomplete.")
        if set(self.history.detail_fields) != {"member_id", "date_of_birth", "drug", "package"}:
            raise ValueError("Request detail fields are incomplete.")
        if set(self.history.open_statuses) & set(self.history.closed_statuses):
            raise ValueError("A status cannot be both open and closed.")
        summary = re.compile(self.history.summary_pattern)
        if not {"first", "last", "total"} <= summary.groupindex.keys():
            raise ValueError("The summary must prove pagination bounds.")
        re.compile(self.history.receipt_pattern)
        return self


def load_recovery_settings(path: Path | None = None) -> RecoverySettings:
    try:
        return RecoverySettings.model_validate(
            load_object(path or DEFAULT_RECOVERY_SETTINGS, "recovery.config", 1048576)
        )
    except (ValidationError, re.error):
        raise InputProblem(
            Issue(
                "recovery.config",
                "recovery_config_invalid",
                "The reviewed recovery configuration is invalid.",
                "Restore the reviewed recovery rules before accessing the portal.",
            )
        ) from None


class RequestMatch(StrictModel):
    request_id: str
    state: Literal["open", "closed", "unknown"]
    submitted: bool = False
    confirmation_id: str | None = None

    @model_validator(mode="after")
    def check_receipt(self) -> "RequestMatch":
        if self.submitted and (not self.confirmation_id or not self.confirmation_id.strip()):
            raise ValueError("A submitted request needs a verified receipt.")
        return self


class History(StrictModel):
    case_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requests: list[RequestMatch]
    complete: bool
    checked_at: float


def case_fingerprints(
    portal_url: str,
    credentials: Credentials,
    facts: dict[str, JsonValue],
    settings: RecoverySettings,
) -> tuple[str, str]:
    # Origin excludes the start page, query and output path.
    validate_url(portal_url)
    parsed = urlsplit(portal_url)
    scope = (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    identity: dict[str, JsonValue] = {}
    for field in settings.identity_fields:
        try:
            value = get_fact(facts, field)
        except KeyError:
            raise InputProblem(
                Issue(
                    "recovery.config",
                    "identity_field_missing",
                    "A case identity field is absent or unsupported.",
                    "Review the identity configuration against the supplied record contract.",
                )
            ) from None
        if not isinstance(value, str) or not value:
            raise state_problem("identity_invalid", "Case identity fields must contain supplied, nonblank strings.")
        identity[field] = normalize(value)
    key = digest(
        {
            "version": "case-v1",
            "portal": scope,
            "account": credentials.username.get_secret_value(),
            "identity": identity,
        }
    )
    return key, digest(facts)


class RecoveryGuard:
    def __init__(
        self,
        directory: Path,
        portal_url: str,
        credentials: Credentials,
        facts: dict[str, JsonValue],
        settings: RecoverySettings,
        log: ActionLog,
    ) -> None:
        key, record_digest = case_fingerprints(portal_url, credentials, facts, settings)
        self.store = JournalStore(directory, key, record_digest, settings.max_journal_bytes)
        self.settings = settings
        self.facts = copy.deepcopy(facts)
        self.log = log
        self.created_here = False
        self.failed = False

    def __enter__(self) -> "RecoveryGuard":
        self.store.__enter__()
        try:
            self.journal = self.store.load()
            self.log.event("case_locked", observation=self.journal.case_key)
            return self
        except BaseException:
            self.store.__exit__(None, None, None)
            raise

    def __exit__(self, *args: object) -> None:
        self.store.__exit__(*args)

    def _active(self) -> None:
        if self.failed or self.store.lock is None or not self.store.lock.is_locked:
            raise state_problem("guard_unavailable", "The case guard has failed or no longer owns its lock.")

    def _persist(self, **updates: object) -> None:
        self._active()
        proposed = type(self.journal).model_validate(copy.deepcopy({**self.journal.model_dump(), **updates}))
        try:
            self.store.save(proposed)
        except InputProblem:
            self.failed = True
            raise
        self.journal = proposed

    def cached_outcome(self) -> RunResult | None:
        self._active()
        if self.journal.phase == "RESOLVED":
            self.log.event("outcome_replayed")
            return self.journal.outcome.model_copy(deep=True) if self.journal.outcome is not None else None
        return None

    def _proof(self, history: History) -> None:
        self._active()
        if history.case_key != self.journal.case_key or history.record_digest != self.journal.record_digest:
            raise state_problem(
                "history_case_mismatch", "The request-history evidence belongs to another case or record."
            )
        age = time.monotonic() - history.checked_at
        if not history.complete or age < 0 or age > self.settings.evidence_max_age_seconds:
            raise InputProblem(
                Issue(
                    "portal.requests",
                    "history_incomplete",
                    "A fresh, complete request-history check is unavailable.",
                    "Review the full request history before creating or sending a request.",
                )
            )
        if len({request.request_id for request in history.requests}) != len(history.requests):
            raise state_problem("history_ambiguous", "Request history contains ambiguous duplicate request IDs.")
        if any(request.state == "unknown" for request in history.requests):
            raise InputProblem(
                Issue(
                    "portal.requests",
                    "request_status_unknown",
                    "A matching request has an unreviewed status.",
                    "Review the existing request status manually before continuing.",
                )
            )

    def inspect(self, history: History) -> RunResult | None:
        self._proof(history)
        if self.journal.phase == "SEND_PENDING":
            matching = [request for request in history.requests if request.request_id == self.journal.request_id]
            if len(matching) == 1 and matching[0].submitted:
                result = RunResult(
                    status="SUCCESS",
                    reason="Recovered the portal confirmation; no submission was retried.",
                    confirmation_id=matching[0].confirmation_id or "",
                    fields_filled=self.journal.fields_filled,
                )
                self.finish(result, request_id=matching[0].request_id)
                self.log.event("submission_reconciled")
                return result
        if self.journal.phase != "NEW":
            raise state_problem(
                "pending_write",
                "A prior request write is unresolved; it cannot be repeated automatically.",
            )
        if any(request.state == "open" for request in history.requests):
            raise InputProblem(
                Issue(
                    "portal.request",
                    "existing_open_request",
                    "An open request already exists for this patient and medication.",
                    "Review the existing request; do not create another request.",
                )
            )
        self.log.event("duplicate_check_clear")
        return None

    def before_create(self, history: History) -> None:
        if self.journal.phase != "NEW":
            raise state_problem("pending_write", "A request write has already been authorized for this case.")
        self.inspect(history)
        self._persist(phase="CREATE_PENDING")
        self.log.event("create_intent_persisted")

    def created(self, request_id: str) -> None:
        if self.journal.phase != "CREATE_PENDING" or not request_id.strip():
            raise state_problem("transition_invalid", "Request creation was not authorized or its ID is missing.")
        self._persist(phase="DRAFT", request_id=request_id)
        self.created_here = True
        self.log.event("request_creation_verified")

    def before_send(self, history: History, fields_filled: dict[str, JsonValue]) -> None:
        self._proof(history)
        if self.journal.phase != "DRAFT" or not self.created_here:
            raise state_problem("send_not_authorized", "Only this run's verified draft may proceed to Send.")
        matching = [request for request in history.requests if request.request_id == self.journal.request_id]
        if (
            len(matching) != 1
            or matching[0].state != "open"
            or matching[0].confirmation_id
            or any(
                request.state == "open" and request.request_id != self.journal.request_id
                for request in history.requests
            )
        ):
            raise state_problem("send_not_authorized", "The draft or its duplicate check is no longer safe to send.")
        for field, value in fields_filled.items():
            try:
                source = get_fact(self.facts, field)
            except KeyError:
                raise state_problem("unsupported_filled_field", "A filled field has no supplied source fact.") from None
            if digest(source) != digest(value):
                raise state_problem("changed_filled_value", "A filled value differs from its supplied source fact.")
        self._persist(phase="SEND_PENDING", fields_filled=fields_filled)
        self.log.event("send_intent_persisted")

    def finish(self, result: RunResult, *, request_id: str) -> None:
        if (
            self.journal.phase != "SEND_PENDING"
            or result.status == "ESCALATED"
            or request_id != self.journal.request_id
        ):
            raise state_problem("transition_invalid", "A terminal outcome requires a pending, verified submission.")
        if digest(result.fields_filled) != digest(self.journal.fields_filled):
            raise state_problem("changed_filled_value", "The final fields differ from the persisted send intent.")
        self._persist(phase="RESOLVED", outcome=result)
        self.log.event("submission_outcome_persisted", status=result.status)
