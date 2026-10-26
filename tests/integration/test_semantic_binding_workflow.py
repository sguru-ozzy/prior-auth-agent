"""A different synthetic UI exercises the complete semantic loop and durable Send guard."""

import html
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from pydantic import JsonValue

from prior_auth_agent.agent.proposals import load_browser_settings
from prior_auth_agent.agent.questions import load_workflow_settings
from prior_auth_agent.agent.recovery import RecoveryGuard, load_recovery_settings
from prior_auth_agent.browser.session import PortalBrowser
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.semantic.contracts import load_semantic_settings
from prior_auth_agent.semantic.workflow import SemanticWorkflow
from prior_auth_agent.storage.audit import ActionLog
from prior_auth_agent.validation import get_fact
from tests.unit.semantic_support import binding_reply, harness


def respond(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if "candidates" in payload:
        return dict(observation=payload["observation"], candidate_id=payload["candidates"][0]["candidate_id"])
    label = payload["field"]["label"].lower()
    source = (
        "patient.member_id/text"
        if "identifier" in label
        else "patient.first_name/text"
        if "forename" in label
        else "patient.last_name/text"
        if "surname" in label
        else "medication.name/text"
        if "medication" in label
        else "clinical.quantity_per_28_days/text"
    )
    return binding_reply(source)(payload)


@contextmanager
def synthetic_portal(record: dict, variant: str) -> Iterator[tuple[str, dict]]:
    state = dict(creates=0, sends=0, answers={}, name="", status="Draft", reads=[], values={})
    settings = load_semantic_settings()
    fields = settings.required_review_fields + ["coverage.plan_name"]
    details = "".join(
        "<dt>"
        + html.escape(
            "Insurance member identifier" if field == "patient.member_id" else settings.detail_labels[field][0]
        )
        + "</dt><dd>"
        + html.escape(str(get_fact(record, field)))
        + "</dd>"
        for field in reversed(fields)
    )
    nav = '<a href="/history">Requests</a><a href="/intake">New request</a>'
    summary = '<section aria-label="Patient identifiers"><dl>' + details + "</dl></section>"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def page(self, body: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(("<!doctype html><html><body>" + body + "</body></html>").encode())

        def do_GET(self) -> None:
            state["reads"].append(self.path)
            if self.path == "/":
                self.page(
                    '<form method="post" action="/signin"><label>Username<input name="u"></label>'
                    '<label>Password<input name="p" type="password"></label><button>Sign in</button></form>'
                )
            elif self.path == "/home":
                self.page("<h1>Workspace</h1>" + nav)
            elif self.path == "/history":
                if not state["creates"]:
                    self.page(nav + "<p>No requests</p>")
                else:
                    self.page(
                        nav + "<p>Showing 1-1 of 1</p><table><thead><tr>"
                        "<th>Status</th><th>Medication name</th><th>Request ID</th></tr></thead><tbody><tr>"
                        "<td>" + state["status"] + "</td><td>" + html.escape(record["medication"]["name"]) + "</td>"
                        '<td><a href="/case">CASE-1</a></td></tr></tbody></table>'
                    )
            elif self.path == "/intake":
                subject = "Prescriber details" if variant == "wrong-subject" else "Patient details"
                self.page(
                    nav + summary + '<form method="post" action="/create"><fieldset><legend>' + subject + "</legend>"
                    '<label>Legal surname<input name="family" required></label>'
                    '<label>Legal forename<input name="given" required></label></fieldset>'
                    "<button>Create request</button></form>"
                )
            elif self.path in ("/case", "/questionnaire"):
                identity = "<dl><dt>Request ID</dt><dd>CASE-1</dd><dt>Status</dt><dd>" + state["status"] + "</dd></dl>"
                if state["sends"]:
                    self.page(nav + identity + summary + "<dl><dt>Confirmation number</dt><dd>CONF-1</dd></dl>")
                elif self.path == "/case":
                    self.page(
                        nav + identity + summary + '<a href="/questionnaire">Questions</a>'
                        '<form method="post" action="/send"><label>Your full name<input name="attestation" value="'
                        + html.escape(state["name"])
                        + '"></label><p>By typing your name you attest that the information '
                        "is accurate.</p><button>Send to plan</button></form>"
                    )
                else:
                    days = "30" if variant == "wrong-units" else "28"
                    label = "Total quantity for 28 days" if variant == "reordered" else "Units requested for cycle"
                    value = state["answers"].get("quantity", [""])[0]
                    self.page(
                        nav
                        + identity
                        + '<form method="post" action="/save"><fieldset><legend>Clinical questions</legend>'
                        '<label for="q">' + label + '</label><input id="q" name="quantity" type="number" required '
                        'aria-describedby="period" value="' + html.escape(value) + '">'
                        '<p id="period">Cycle length is ' + days + " days.</p></fieldset><button>Save answers</button>"
                        '</form><a href="/case">Review</a>'
                    )
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            values = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            destination = "/home"
            if self.path == "/create":
                state["creates"] += 1
                state["values"] = values
                destination = "/case"
            elif self.path == "/save":
                state["answers"] = values
                destination = "/questionnaire"
            elif self.path == "/send":
                state["sends"] += 1
                state["name"] = values["attestation"][0]
                state["status"] = "Sent to plan"
                destination = "/case"
            self.send_response(303)
            self.send_header("Location", destination)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("variant", ["help-text", "reordered", "wrong-subject", "wrong-units"])
def test_complete_unfamiliar_workflow_or_safe_handoff(tmp_path: Path, record: dict, variant: str) -> None:
    browser_settings = load_browser_settings()
    semantic_settings = load_semantic_settings().model_copy(update={"settle_ms": 50})
    credentials = Credentials(username="SYNTHETIC_LOGIN", password="SYNTHETIC_PASSWORD")
    with synthetic_portal(record, variant) as (url, state), harness(tmp_path / "model", record, respond) as model:
        log = ActionLog(tmp_path / "workflow.jsonl")
        try:
            with RecoveryGuard(tmp_path / "state", url, credentials, record, load_recovery_settings(), log) as guard:
                with PortalBrowser(url, [], browser_settings, time.monotonic() + 70, log, semantic=True) as browser:
                    workflow = SemanticWorkflow(
                        browser,
                        guard,
                        semantic_settings,
                        browser_settings,
                        load_workflow_settings(),
                        record,
                        credentials,
                        log,
                        model.planner.model,
                    )
                    if variant in ("wrong-subject", "wrong-units"):
                        with pytest.raises(InputProblem) as raised:
                            workflow.run(complete=True)
                        assert raised.value.issue.code == "question_unreviewed"
                        assert state["sends"] == 0
                        assert state["creates"] == (0 if variant == "wrong-subject" else 1)
                    else:
                        result = workflow.run(complete=True)
                        assert result.status == "SUCCESS"
                        assert result.confirmation_id == "CONF-1"
                        assert state["creates"] == state["sends"] == 1
                        assert state["answers"]["quantity"] == [str(record["clinical"]["quantity_per_28_days"])]
                        assert state["values"]["given"] == [record["patient"]["first_name"]]
                        assert state["values"]["family"] == [record["patient"]["last_name"]]
                        assert state["name"] == record["attestation_name"]
                        assert guard.cached_outcome() == result
            assert not any(path.startswith(("/__state", "/__control", "/api/")) for path in state["reads"])
        finally:
            log.close()
    events = (tmp_path / "workflow.jsonl").read_text()
    if variant not in ("wrong-subject", "wrong-units"):
        assert "semantic_binding_authorized" in events
        assert "semantic_saved_answers_verified" in events
        ordered = [json.loads(line) for line in events.splitlines()]
        saved = next(i for i, event in enumerate(ordered) if event["event"] == "semantic_saved_answers_verified")
        attest = next(
            i
            for i, event in enumerate(ordered)
            if event["event"] == "semantic_action_authorized" and event.get("binding") == "attestation_name"
        )
        assert saved < attest
        assert record["patient"]["member_id"] not in events
        assert record["patient"]["first_name"] not in events
        assert record["patient"]["member_id"] not in json.dumps(model.captured)


def test_changed_dom_subject_blocks_proposed_action(tmp_path: Path, record: dict) -> None:
    settings = load_browser_settings()
    credentials = Credentials(username="SYNTHETIC_LOGIN", password="SYNTHETIC_PASSWORD")
    with synthetic_portal(record, "help-text") as (url, state), harness(tmp_path / "model", record, respond) as model:
        log = ActionLog(tmp_path / "workflow.jsonl")
        try:
            with RecoveryGuard(tmp_path / "state", url, credentials, record, load_recovery_settings(), log) as guard:
                with PortalBrowser(url + "intake", [], settings, time.monotonic() + 30, log, semantic=True) as browser:
                    workflow = SemanticWorkflow(
                        browser,
                        guard,
                        load_semantic_settings(),
                        settings,
                        load_workflow_settings(),
                        record,
                        credentials,
                        log,
                        model.planner.model,
                    )
                    observed = workflow.ui.stable()
                    actions, _ = workflow.policy.fill_actions(observed)
                    browser.page.locator("legend").evaluate("e => e.textContent = 'Prescriber details'")
                    with pytest.raises(InputProblem) as raised:
                        workflow.ui.perform(observed, actions[0], write=True)
                    assert raised.value.issue.code == "stale_proposal"
                    assert browser.page.locator('input[name="given"]').input_value() == ""
                    assert state["creates"] == state["sends"] == 0
        finally:
            log.close()
