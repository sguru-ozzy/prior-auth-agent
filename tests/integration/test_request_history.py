"""Real-browser duplicate scans and read-only interrupted-send reconciliation."""

import html
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import JsonValue

from prior_auth_agent.agent.inspection import inspection_loop
from prior_auth_agent.agent.planner import ReviewedPlanner
from prior_auth_agent.agent.proposals import load_browser_settings
from prior_auth_agent.agent.recovery import (
    History,
    RecoveryGuard,
    RequestMatch,
    case_fingerprints,
    load_recovery_settings,
)
from prior_auth_agent.browser.requests import RequestHistoryReader
from prior_auth_agent.browser.session import PortalBrowser
from prior_auth_agent.cli import main
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.storage.audit import ActionLog


@dataclass(frozen=True)
class ExistingRequest:
    key: str
    member: str
    dob: str
    drug: str
    package: str
    status: str = "Draft"
    receipt: str = ""


def existing(record: dict[str, JsonValue], key: str = "REQ-1", **changes: str) -> ExistingRequest:
    patient, medication = record["patient"], record["medication"]
    assert isinstance(patient, dict) and isinstance(medication, dict)
    values = {
        "key": key,
        "member": patient["member_id"],
        "dob": patient["date_of_birth"],
        "drug": medication["name"],
        "package": medication["package"],
    }
    values.update(changes)
    return ExistingRequest(**values)


@contextmanager
def portal(requests: list[ExistingRequest], scenario: str = "") -> Iterator[tuple[str, list[str], list[str]]]:
    writes: list[str] = []
    reads: list[str] = []
    rules = load_recovery_settings().history

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def page(self, body: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self) -> None:
            reads.append(self.path)
            path = urlsplit(self.path).path
            if path == "/dashboard":
                self.page('<h1>Dashboard</h1><a href="/requests">Requests</a>')
            elif path == "/password":
                self.page(
                    '<form action="/signin/password" method="post">'
                    '<label>Password<input type="password"></label><button>Sign in</button></form>'
                )
            elif path == "/requests":
                number = int(parse_qs(urlsplit(self.path).query).get("page", ["1"])[0])
                total = len(requests)
                first = last = number if total else 0
                filters = "".join(
                    '<input aria-label="' + html.escape(label) + '" value="">' for label in rules.filter_labels
                )
                statuses = "".join(
                    '<button aria-pressed="'
                    + ("true" if scenario == "filtered" and status == "Draft" else "false")
                    + '">'
                    + html.escape(status)
                    + "</button>"
                    for status in rules.open_statuses + rules.closed_statuses
                )
                rows = ""
                if total:
                    item = requests[number - 1]
                    rows = (
                        '<tr><td><a href="/requests/'
                        + item.key
                        + '">'
                        + item.key
                        + "</a></td><td>"
                        + html.escape(item.drug)
                        + "</td><td>"
                        + html.escape(item.status)
                        + "</td></tr>"
                    )
                disabled = "disabled" if number >= total or scenario == "truncated" else ""
                self.page(
                    "<main><h1>Requests</h1>" + filters + statuses + "<button>Clear filters</button>"
                    "<table><thead><tr><th>Key</th><th>Drug</th><th>Status</th></tr></thead><tbody>"
                    + rows
                    + "</tbody></table><span>Showing "
                    + str(first)
                    + "–"
                    + str(last)
                    + " of "
                    + str(total)
                    + "</span>"
                    "<button "
                    + disabled
                    + " onclick=\"location.href='/requests?page="
                    + str(number + 1)
                    + "'\">Next</button></main>"
                )
            elif path.startswith("/requests/"):
                key = path.rsplit("/", 1)[1]
                item = next(item for item in requests if item.key == key)
                values = {
                    "Member ID": item.member,
                    "Date of birth": item.dob,
                    "Drug": item.drug,
                    "Package": item.package,
                }
                terms = "".join(
                    "<dt>" + html.escape(label) + "</dt><dd>" + html.escape(value) + "</dd>"
                    for label, value in values.items()
                )
                detail_script = ""
                if scenario == "slow-details":
                    detail_script = (
                        "<script>setTimeout(()=>document.querySelector('dl').innerHTML="
                        + json.dumps(terms)
                        + ",300)</script>"
                    )
                    terms = ""
                self.page(
                    "<main><h1>"
                    + item.key
                    + " "
                    + html.escape(item.status)
                    + "</h1><dl>"
                    + terms
                    + "</dl><p>"
                    + html.escape(item.receipt)
                    + "</p></main>"
                    + detail_script
                )
            else:
                self.page(
                    '<form action="/signin" method="post">'
                    '<label>Username<input name="username" type="text"></label><button>Continue</button></form>'
                )

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            writes.append(self.path)
            self.send_response(303)
            self.send_header("Location", "/password" if self.path == "/signin" else "/dashboard")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", writes, reads
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def read_history(url: str, record: dict[str, JsonValue], tmp_path: Path, request_id: str | None = None) -> History:
    settings = load_browser_settings()
    credentials = Credentials(username="synthetic", password="synthetic")
    log = ActionLog(tmp_path / "reader.actions.jsonl")
    try:
        with PortalBrowser(url, [], settings, time.monotonic() + 30) as browser:
            inspection_loop(browser, ReviewedPlanner(), settings, credentials, log, False, time.monotonic() + 30)
            return RequestHistoryReader(browser, load_recovery_settings().history, log).read(
                record, case_fingerprints(url, credentials, record, load_recovery_settings())[0], request_id
            )
    finally:
        log.close()


def test_all_pages_are_checked_and_duplicate_is_found_on_later_page(
    tmp_path: Path,
    record: dict[str, JsonValue],
) -> None:
    requests = [existing(record, "OTHER", drug="Other medication"), existing(record)]
    with portal(requests) as (url, writes, reads):
        result = read_history(url, record, tmp_path)
        assert result.complete
        assert [item.request_id for item in result.requests] == ["REQ-1"]
        assert result.requests[0].state == "open"
        assert "/requests?page=2" in reads
        assert "/requests/OTHER" not in reads
        assert writes == ["/signin", "/signin/password"]


@pytest.mark.parametrize("scenario", ["filtered", "truncated"])
def test_partial_or_filtered_history_never_becomes_a_clear_check(
    tmp_path: Path,
    record: dict[str, JsonValue],
    scenario: str,
) -> None:
    with portal([existing(record), existing(record, "REQ-2")], scenario) as (url, writes, _):
        with pytest.raises(InputProblem):
            read_history(url, record, tmp_path)
        assert writes == ["/signin", "/signin/password"]


def test_slow_details_are_awaited_before_duplicate_decision(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal([existing(record)], "slow-details") as (url, writes, _):
        result = read_history(url, record, tmp_path)
        assert result.complete and result.requests[0].state == "open"
        assert writes == ["/signin", "/signin/password"]


def test_empty_unfiltered_history_can_be_verified(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal([]) as (url, writes, _):
        result = read_history(url, record, tmp_path)
        assert result.complete and not result.requests
        assert writes == ["/signin", "/signin/password"]


def test_different_patient_is_not_a_duplicate(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal([existing(record, member="ZZ399999")]) as (url, _, _):
        assert read_history(url, record, tmp_path).requests == []


def test_matching_member_with_wrong_dob_escalates(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal([existing(record, dob="1901-01-01")]) as (url, _, _):
        with pytest.raises(InputProblem) as raised:
            read_history(url, record, tmp_path)
        assert raised.value.issue.field == "patient.date_of_birth"


def test_ambiguous_receipts_cannot_reconcile(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal([existing(record, status="Sent to plan", receipt="QMS-ONE QMS-TWO")]) as (url, _, _):
        with pytest.raises(InputProblem):
            read_history(url, record, tmp_path, "REQ-1")


def test_package_change_does_not_hide_an_open_duplicate(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal([existing(record, package="Different package")]) as (url, _, _):
        result = read_history(url, record, tmp_path)
        assert result.requests[0].state == "open"


def test_interrupted_send_reconciles_from_readonly_browser_evidence(
    tmp_path: Path,
    record: dict[str, JsonValue],
) -> None:
    with portal([existing(record, status="Sent to plan", receipt="QMS-RECOVERED")]) as (url, writes, _):
        log = ActionLog(tmp_path / "recovery.actions.jsonl")
        credentials = Credentials(username="synthetic", password="synthetic")
        settings = load_recovery_settings()
        try:
            with RecoveryGuard(tmp_path / "state", url, credentials, record, settings, log) as current:
                current.before_create(
                    History(
                        case_key=current.journal.case_key,
                        record_digest=current.journal.record_digest,
                        requests=[],
                        complete=True,
                        checked_at=time.monotonic(),
                    )
                )
                current.created("REQ-1")
                current.before_send(
                    History(
                        case_key=current.journal.case_key,
                        record_digest=current.journal.record_digest,
                        requests=[RequestMatch(request_id="REQ-1", state="open")],
                        complete=True,
                        checked_at=time.monotonic(),
                    ),
                    {},
                )
            evidence = read_history(url, record, tmp_path, "REQ-1")
            with RecoveryGuard(tmp_path / "state", url, credentials, record, settings, log) as restarted:
                result = restarted.inspect(evidence)
                assert result is not None and result.status == "SUCCESS"
                assert result.confirmation_id == "QMS-RECOVERED"
            assert writes == ["/signin", "/signin/password"]
        finally:
            log.close()


def test_cli_escalates_open_duplicate_without_creating_a_request(
    tmp_path: Path,
    record: dict[str, JsonValue],
) -> None:
    patient, credentials, output = tmp_path / "patient.json", tmp_path / "credentials.json", tmp_path / "result.json"
    patient.write_text(json.dumps(record))
    credentials.write_text('{"username":"synthetic","password":"synthetic"}')
    with portal([existing(record)]) as (url, writes, _):
        assert (
            main(
                [
                    "--portal-url",
                    url,
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
            )
            == 0
        )
        result = json.loads(output.read_text())
        assert result["status"] == "ESCALATED"
        assert result["escalation"]["field"] == "portal.request"
        assert writes == ["/signin", "/signin/password"]


def test_another_terminal_status_does_not_guess_a_submission_outcome(
    tmp_path: Path,
    record: dict[str, JsonValue],
) -> None:
    with portal([existing(record, status="Denied", receipt="QMS-OLDER")]) as (url, _, _):
        result = read_history(url, record, tmp_path, "REQ-1")
        assert result.requests[0].state == "closed"
        assert result.requests[0].submitted is False
