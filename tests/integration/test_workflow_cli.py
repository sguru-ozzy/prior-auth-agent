"""Complete CLI runs against a synthetic portal with independent persisted server state."""

import copy
import html
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Error as BrowserError
from pydantic import JsonValue

from prior_auth_agent.agent.proposals import DEFAULT_BROWSER_SETTINGS
from prior_auth_agent.agent.recovery import load_recovery_settings
from prior_auth_agent.browser.workflow import RequestWorkflow
from prior_auth_agent.cli import main

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workflow"

QUESTIONS = [
    {
        "id": "diagnosis",
        "text": "Primary diagnosis (ICD-10 code)",
        "kind": "text",
        "required": True,
        "choices": [],
        "show_if": None,
        "hint": "",
    },
    {
        "id": "new",
        "text": "Is this a new start of therapy?",
        "kind": "radio",
        "required": True,
        "choices": ["Yes", "No"],
        "show_if": None,
        "hint": "",
    },
    {
        "id": "quantity",
        "text": "Quantity per 28 days",
        "kind": "number",
        "required": True,
        "choices": [],
        "show_if": None,
        "hint": "",
    },
    {
        "id": "benefit",
        "text": "Which benefit covers this request?",
        "kind": "radio",
        "required": True,
        "choices": ["Part B", "Part D"],
        "show_if": None,
        "hint": "",
    },
    {
        "id": "notes",
        "text": "Anything else the reviewer should know",
        "kind": "textarea",
        "required": False,
        "choices": [],
        "show_if": None,
        "hint": "",
    },
]


@contextmanager
def portal(record: dict[str, JsonValue], scenario: str = "") -> Iterator[tuple[str, dict[str, JsonValue]]]:
    facts = copy.deepcopy(record)
    state: dict[str, JsonValue] = {
        "creates": 0,
        "sends": 0,
        "saved": False,
        "answers": {},
        "status": "Draft",
        "wizard": {},
        "search_reads": 0,
        "send_attempts": 0,
        "history_reads": 0,
    }
    recovery = load_recovery_settings().history
    patient = {**facts["patient"], "gender": "Female"}
    drug = {"label": facts["medication"]["name"], "package": facts["medication"]["package"]}
    plan = {**facts["coverage"], "plan_id": "plan", "name": facts["coverage"]["plan_name"]}
    form = {
        "title": "Matched prior authorization",
        "plan_id": "plan",
        "plan": plan["name"],
        "electronic": scenario not in ("fax", "missing-clinical-facts", "late-missing-questions"),
    }
    questions = copy.deepcopy(QUESTIONS)
    if scenario == "alternate-layout":
        questions[0]["text"] = "ICD-10 diagnosis code"
        questions[1]["text"] = "Is the patient starting this therapy?"
        questions[2]["text"] = "Requested quantity per 28 days"
    if scenario == "unknown-question":
        questions.append(
            {
                "id": "unknown",
                "text": "Has the patient had tuberculosis screening?",
                "kind": "radio",
                "required": True,
                "choices": ["Yes", "No"],
                "show_if": None,
                "hint": "",
            }
        )

    if scenario == "missing-clinical-facts":
        questions.extend(
            [
                {
                    "id": "tb",
                    "text": "Has the patient had a tuberculosis screening in the last 12 months?",
                    "kind": "radio",
                    "required": True,
                    "choices": ["Yes", "No"],
                    "show_if": None,
                    "hint": "",
                },
                {
                    "id": "severity",
                    "text": "Current disease severity",
                    "kind": "radio",
                    "required": True,
                    "choices": ["Mild", "Moderate", "Severe"],
                    "show_if": None,
                    "hint": "",
                },
            ]
        )

    def intake() -> str:
        text_fields = {
            "first_name": "First name",
            "last_name": "Last name",
            "date_of_birth": "Date of birth",
            "member_id": "Member ID",
            "zip_code": "ZIP code",
        }
        controls = "".join(
            "<label>"
            + label
            + '<input type="'
            + ("date" if key == "date_of_birth" else "text")
            + '" oninput="patientValue(\''
            + key
            + "',this.value)\"></label>"
            for key, label in text_fields.items()
        )
        controls += (
            '<button id="gender" aria-label="Gender" onclick="patientOptionsFor(\'gender\')">Select gender</button>'
        )
        controls += '<button id="state" aria-label="State" onclick="patientOptionsFor(\'state\')">Select state</button>'
        controls += '<div id="patient-options"></div>'
        if scenario == "alternate-layout":
            controls = "".join(
                "<label>"
                + label
                + '<input type="'
                + ("date" if key == "date_of_birth" else "text")
                + '" oninput="patientValue(\''
                + key
                + "',this.value)\"></label>"
                for key, label in text_fields.items()
            )
            controls += (
                "<label>Gender<select aria-label='Gender' onchange=\"patientValue('gender',this.value)\">"
                '<option value="">Choose</option>'
                "<option>Female</option>"
                "<option>Male</option>"
                "</select></label>"
            )
            controls += (
                "<label>State<select aria-label='State' onchange=\"patientValue('state',this.value)\">"
                '<option value="">Choose</option>'
                '<option value="OH">OH · Ohio</option>'
                '<option value="PA">PA · Pennsylvania</option>'
                "</select></label>"
            )
        return (
            (FIXTURES / "intake.html")
            .read_text(encoding="utf-8")
            .replace("NEW_PATIENT_CONTROLS", json.dumps(controls))
            .replace("NO_PATIENT", "true" if scenario in ("new-patient", "new-patient-mismatch") else "false")
            .replace("AMBIGUOUS_PATIENT", "true" if scenario == "ambiguous-patient" else "false")
            .replace("PATIENT", json.dumps(patient))
            .replace("DRUG", json.dumps(drug))
            .replace("PLAN", json.dumps(plan))
            .replace("FORM", json.dumps(form))
        )

    def summary() -> str:
        receipt = "<p>Confirmation QMS-TEST</p>" if state["status"] == "Sent to plan" else ""
        return (
            "<h1>REQ-TEST "
            + str(state["status"])
            + "</h1>"
            + receipt
            + (FIXTURES / "summary.html")
            .read_text(encoding="utf-8")
            .replace(
                "TERMS",
                "".join(
                    "<dt>" + key + "</dt><dd>" + html.escape(str(value)) + "</dd>"
                    for key, value in {
                        "Member ID": facts["patient"]["member_id"],
                        "Date of birth": facts["patient"]["date_of_birth"],
                        "Drug": facts["medication"]["name"],
                        "Package": facts["medication"]["package"],
                    }.items()
                ),
            )
            .replace("OPEN_LABEL", "Send to plan" if form["electronic"] else "Fax to plan")
            .replace(
                "MODAL",
                json.dumps(modal()),
            )
        )

    def modal() -> str:
        interrupt = {
            "unexpected-popup": "window.open('/unreviewed-popup');",
            "blocked-write": "fetch('/unapproved-write',{method:'POST'});",
        }.get(scenario, "")
        missing = scenario == "late-missing-questions"
        warning = (
            (
                "<section><p>Answer every required question before sending:</p><ul>"
                "<li>Has the patient had a tuberculosis screening in the last 12 months?</li>"
                "<li>Current disease severity</li></ul></section>"
            )
            if missing
            else ""
        )
        route = "Send to plan" if form["electronic"] else "Fax to plan"
        return (
            '<div role="dialog" aria-label="Attestation">'
            + warning
            + '<label>Your full name<input id="name" oninput="document.getElementById(\'confirm\').disabled='
            + ("true" if missing else "!this.value.trim()")
            + ";"
            + interrupt
            + '"></label>'
            "<p>By typing your name you attest that the information in this request is accurate.</p>"
            '<button id="confirm" disabled onclick="send()">' + route + "</button></div>"
        )

    def question_page() -> str:
        controls = ""
        for question in questions:
            text, key = question["text"], question["id"]
            if question["kind"] == "radio":
                controls += '<div role="radiogroup" aria-label="' + text + '">'
                for choice in question["choices"]:
                    controls += (
                        '<label><input type="radio" name="' + key + '" value="' + choice + '">' + choice + "</label>"
                    )
                controls += "</div>"
            else:
                input_type = ' type="number"' if scenario == "alternate-layout" and question["kind"] == "number" else ""
                controls += "<label>" + text + "<input" + input_type + ' id="' + key + '"></label>'
        return (
            '<h1>REQ-TEST Draft</h1><button role="tab">Questions</button>'
            + controls
            + (FIXTURES / "questions.html").read_text(encoding="utf-8").replace("QUESTIONS", json.dumps(questions))
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def respond(self, body: str, kind: str = "text/html; charset=utf-8", status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body.encode())

        def json(self, value: object) -> None:
            self.respond(json.dumps(value), "application/json")

        def page(self, content: str) -> None:
            overlay = ""
            if scenario in ("known-overlay", "recurring-overlay", "unknown-overlay") and self.path == "/new":
                message = (
                    "Quillmere uses cookies to keep you signed in and, with your permission, "
                    "to measure how the site is used."
                )
                if scenario == "unknown-overlay":
                    message = "Attest that this clinical request is accurate before continuing."
                dismiss = "" if scenario == "recurring-overlay" else "this.parentElement.remove()"
                overlay = (
                    '<div style="position:fixed;inset:0;z-index:100;background:white"><p>'
                    + message
                    + '</p><button>Preferences</button><button onclick="'
                    + dismiss
                    + '">Reject non-essential</button><button>Accept all</button></div>'
                )
            if scenario == "alternate-layout" and self.path == "/new":
                for old, new in (
                    ("First name", "Given name"),
                    ("Last name", "Family name"),
                    ("Date of birth", "DOB"),
                    ("ZIP code", "Postal code"),
                    ("Member ID", "Insurance member ID"),
                    ("Search patients", "Find patient"),
                    ("1 Patient", "Patient information"),
                    ("2 Medication", "Requested medication"),
                    ("3 Plan and form", "Coverage"),
                    (">BIN<", ">Rx BIN<"),
                    (">PCN<", ">Rx PCN<"),
                    (">Group<", ">Rx group<"),
                    ("Find forms", "Find plan forms"),
                    ("Create request", "Create authorization request"),
                    ("New patient", "Enter new patient"),
                ):
                    content = content.replace(old, new)
            self.respond(
                '<nav><a href="/requests">Requests</a><a href="/new">New request</a></nav><main>'
                + content
                + overlay
                + "</main>"
            )

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/signin":
                self.page(
                    '<form action="/signin" method="post"><label>Username<input type="text"></label>'
                    "<button>Continue</button></form>"
                )
            elif path == "/password":
                self.page(
                    '<form action="/signin/password" method="post"><label>Password<input type="password"></label>'
                    "<button>Sign in</button></form>"
                )
            elif path == "/dashboard":
                self.page(
                    "<h1>Dashboard</h1>"
                    if "session=yes" in self.headers.get("Cookie", "")
                    else '<script>location.href="/signin"</script>'
                )
            elif path == "/new":
                self.page(intake())
            elif path == "/requests":
                filters = "".join('<input aria-label="' + label + '">' for label in recovery.filter_labels)
                statuses = "".join(
                    '<button aria-pressed="false">' + status + "</button>"
                    for status in recovery.open_statuses + recovery.closed_statuses
                )
                total = 1 if state["creates"] else 0
                row = (
                    (
                        '<tr><td><a href="/requests/REQ-TEST">REQ-TEST</a></td><td>'
                        + str(facts["medication"]["name"])
                        + "</td><td>"
                        + str(state["status"])
                        + "</td></tr>"
                    )
                    if total
                    else ""
                )
                state["history_reads"] += 1
                if scenario in ("history-throttled", "history-countdown", "history-unavailable") and (
                    scenario == "history-unavailable" or state["history_reads"] <= 2
                ):
                    message = "The list could not be loaded (no answer)."
                    label = "Try again"
                    if scenario == "history-countdown":
                        message = (
                            "Too many requests. Quillmere is limiting how fast this account can load pages. "
                            "The rest of this page loads in 0 seconds."
                        )
                        label = "Try again now"
                    self.page(
                        "<h1>Requests</h1>"
                        + filters
                        + statuses
                        + '<button>Clear filters</button><div role="alert">'
                        + message
                        + ("" if scenario == "history-countdown" else "</div>")
                        + '<button onclick="location.reload()">'
                        + label
                        + "</button>"
                        + ("</div>" if scenario == "history-countdown" else "")
                    )
                    return
                self.page(
                    "<h1>Requests</h1>"
                    + filters
                    + statuses
                    + "<button>Clear filters</button><table><thead><tr><th>Key</th><th>Drug</th>"
                    "<th>Status</th></tr></thead><tbody>"
                    + row
                    + "</tbody></table><span>Showing "
                    + ("1–1 of 1" if total else "0–0 of 0")
                    + "</span>"
                )
            elif path == "/requests/REQ-TEST":
                self.page(summary())
            elif path == "/requests/REQ-TEST/questions":
                self.page(question_page())
            elif path == "/api/me":
                self.json(
                    {
                        "authenticated": True,
                        "user": {"username": "SYNTHETIC_USER"},
                    }
                )
            elif path == "/api/drugs":
                self.json({"query": parse_qs(urlsplit(self.path).query).get("q", [""])[0], "results": [drug]})
            elif path == "/api/patients":
                state["search_reads"] += 1
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                if scenario == "delayed-search":
                    time.sleep(0.3)
                if scenario == "throttled-search" and state["search_reads"] <= 2:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    self.wfile.write(b"Temporarily throttled")
                    return
                if scenario == "search-unavailable":
                    self.respond("Temporarily unavailable", status=503)
                    return
                results = [] if scenario in ("new-patient", "new-patient-mismatch", "alternate-layout") else [patient]
                if scenario == "ambiguous-patient":
                    results = [patient, patient]
                self.json({"query": "OLD_QUERY" if scenario == "stale-query" else query, "results": results})
            elif path == "/api/wizard":
                self.json({"draft": state["wizard"]})
            elif path == "/api/requests/REQ-TEST":
                p = {key: patient[key] for key in ("first_name", "last_name", "gender", "state", "zip_code")}
                self.json(
                    {
                        "request": {
                            "key": "REQ-TEST",
                            "status": state["status"],
                            "member_id": patient["member_id"],
                            "date_of_birth": patient["date_of_birth"],
                            "patient_detail": p,
                            "plan_detail": plan,
                            "drug": drug["label"],
                            "drug_package": drug["package"],
                            "electronic": form["electronic"],
                        }
                    }
                )
            elif path == "/api/requests/REQ-TEST/questions":
                shown_questions = copy.deepcopy(questions)
                if scenario == "changed-question-schema" and state["saved"]:
                    shown_questions[0]["text"] = "Different unreviewed clinical question"
                self.json(
                    {
                        "rounds": [
                            {
                                "set": {"questions": shown_questions},
                                "saved_at": 1 if state["saved"] else None,
                                "answers": state["answers"],
                                "missing": [],
                            }
                        ]
                    }
                )
            else:
                self.respond("Not found", status=404)

        def body(self) -> dict[str, JsonValue]:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            return json.loads(raw) if raw else {}

        def do_PUT(self) -> None:
            payload = self.body()
            if self.path == "/api/wizard":
                if scenario == "new-patient-mismatch" and payload.get("new_patient", {}).get("zip_code"):
                    payload["new_patient"]["zip_code"] = "99999"
                state["wizard"] = payload
            elif self.path == "/api/requests/REQ-TEST/answers":
                if scenario == "answer-validation":
                    self.respond("Diagnosis is not accepted by this form; verify the supplied code.", status=422)
                    return
                state["answers"] = payload["answers"]
                if scenario == "changed-answer":
                    state["answers"]["diagnosis"] = "UNSUPPORTED"
                state["saved"] = True
            else:
                self.respond("Unknown write", status=404)
                return
            self.json({"ok": True})

        def do_POST(self) -> None:
            payload = self.body()
            if self.path in ("/signin", "/signin/password"):
                self.send_response(303)
                self.send_header("Location", "/password" if self.path == "/signin" else "/dashboard")
                self.send_header("Set-Cookie", "session=yes; HttpOnly; SameSite=Strict")
                self.end_headers()
            elif self.path == "/api/requests":
                journal = next((Path(os.environ["XDG_STATE_HOME"]) / "prior-auth-agent").glob("*.json"))
                state["create_phase"] = json.loads(journal.read_text())["phase"]
                if state["create_phase"] != "CREATE_PENDING":
                    self.respond("No durable Create intent", status=409)
                    return
                state["creates"] += 1
                self.json({"key": "REQ-TEST"})
            elif self.path == "/api/requests/REQ-TEST/send":
                state["send_attempts"] += 1
                journal = next((Path(os.environ["XDG_STATE_HOME"]) / "prior-auth-agent").glob("*.json"))
                state["send_phase"] = json.loads(journal.read_text())["phase"]
                if state["send_phase"] != "SEND_PENDING":
                    self.respond("No durable Send intent", status=409)
                    return
                expected = {
                    "diagnosis": facts["clinical"]["diagnosis_icd10"],
                    "new": "Yes",
                    "quantity": str(facts["clinical"]["quantity_per_28_days"]),
                    "benefit": facts["coverage"]["benefit"],
                    "notes": "",
                }
                if state["answers"] != expected or payload != {
                    "name": facts["attestation_name"],
                    "attested": True,
                }:
                    self.respond("Bad source facts", status=422)
                    return
                if scenario == "send-throttled":
                    self.respond("Temporarily throttled", status=429)
                    return
                if scenario == "send-validation":
                    self.respond("Attestation name is not accepted for this request.", status=422)
                    return
                state["sends"] += 1
                if scenario == "rejected-valid":
                    state["status"] = "FAILED"
                    self.json({"status": "FAILED"})
                    return
                state["status"] = "Sent to plan"
                if scenario == "lost-send-response":
                    self.close_connection = True
                    return
                self.json({"confirmation_id": "QMS-TEST"})
            else:
                self.respond("Unknown write", status=404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/dashboard", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def arguments(tmp_path: Path, record: dict[str, JsonValue], url: str) -> list[str]:
    patient, credentials = tmp_path / "patient.json", tmp_path / "credentials.json"
    patient.write_text(json.dumps(record))
    credentials.write_text('{"username":"SYNTHETIC_USER","password":"SYNTHETIC_PASSWORD"}')
    browser = json.loads(DEFAULT_BROWSER_SETTINGS.read_text())
    browser["wait_ms"] = 1500
    config = tmp_path / "browser.json"
    config.write_text(json.dumps(browser))
    return [
        "--portal-url",
        url,
        "--patient",
        str(patient),
        "--credentials",
        str(credentials),
        "--out",
        str(tmp_path / "result.json"),
        "--as-of",
        "2026-10-25",
        "--browser-config",
        str(config),
    ]


def test_default_cli_completes_one_request_and_replays_without_another_write(
    tmp_path: Path, record: dict[str, JsonValue]
) -> None:
    with portal(record) as (url, state):
        args = arguments(tmp_path, record, url)
        assert main(args) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "SUCCESS", result
        assert result["confirmation_id"] == "QMS-TEST"
        assert result["fields_filled"]["clinical.new_start"] is True
        assert state["creates"] == state["sends"] == 1
        args[args.index("--out") + 1] = str(tmp_path / "replayed.json")
        assert main(args) == 0
        assert json.loads((tmp_path / "replayed.json").read_text()) == result
        assert state["creates"] == state["sends"] == 1


@pytest.mark.parametrize(
    "scenario, field",
    [
        ("unknown-question", "portal.questions"),
        ("changed-answer", "portal.questions"),
        ("changed-question-schema", "portal.questions"),
    ],
)
def test_unsafe_cases_never_send(tmp_path: Path, record: dict[str, JsonValue], scenario: str, field: str) -> None:
    with portal(record, scenario) as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED", result
        assert result["escalation"]["field"] == field, result
        assert state["creates"] == 1 and state["sends"] == 0


def test_lost_send_response_is_reconciled_without_second_send(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal(record, "lost-send-response") as (url, state):
        args = arguments(tmp_path, record, url)
        assert main(args) == 0
        assert state["sends"] == 1
        assert json.loads((tmp_path / "result.json").read_text())["status"] == "ESCALATED"
        assert main(args) == 0
        recovered = json.loads((tmp_path / "result.json").read_text())
        assert recovered["status"] == "SUCCESS", recovered
        assert recovered["confirmation_id"] == "QMS-TEST"
        assert state["creates"] == state["sends"] == 1


def test_invalid_npi_format_stops_before_portal_access(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    record["prescriber"]["npi"] = "INVALID"
    with portal(record) as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["escalation"]["field"] == "prescriber.npi"
        assert state["creates"] == state["sends"] == 0


def test_five_complete_fixture_runs_have_identical_actions_and_results(
    tmp_path: Path, record: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = []
    with portal(record) as (url, state):
        for number in range(5):
            run_dir = tmp_path / str(number)
            run_dir.mkdir()
            monkeypatch.setenv("XDG_STATE_HOME", str(run_dir / "private-state"))
            state.update({"creates": 0, "sends": 0, "saved": False, "answers": {}, "status": "Draft", "wizard": {}})
            assert main(arguments(run_dir, record, url)) == 0
            result = (run_dir / "result.json").read_bytes()
            assert json.loads(result)["status"] == "SUCCESS"
            assert state["creates"] == state["sends"] == 1
            runs.append((result, (run_dir / "result.actions.jsonl").read_bytes()))
    assert all(run == runs[0] for run in runs)


def test_explicit_rejection_of_valid_submission_is_failed_and_replayed(
    tmp_path: Path, record: dict[str, JsonValue]
) -> None:
    with portal(record, "rejected-valid") as (url, state):
        args = arguments(tmp_path, record, url)
        assert main(args) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "FAILED", result
        assert result["confirmation_id"] == ""
        assert state["creates"] == state["sends"] == 1
        assert main(args) == 0
        assert json.loads((tmp_path / "result.json").read_text()) == result
        assert state["creates"] == state["sends"] == 1


@pytest.mark.parametrize("scenario", ["missing-clinical-facts", "late-missing-questions"])
def test_both_missing_clinical_answers_are_reported_without_signing_or_sending(
    tmp_path: Path, record: dict[str, JsonValue], scenario: str
) -> None:
    with portal(record, scenario) as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED"
        assert result["escalation"]["field"] == "portal.questions"
        assert "tuberculosis screening" in result["escalation"]["problem"]
        assert "Current disease severity" in result["escalation"]["problem"]
        assert "attestation_name" not in result["fields_filled"]
        assert state["creates"] == 1 and state["sends"] == 0
        assert "send_intent_persisted" not in (tmp_path / "result.actions.jsonl").read_text()


def test_fax_route_uses_portal_button_and_verifies_receipt(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal(record, "fax") as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "SUCCESS", result
        assert result["confirmation_id"] == "QMS-TEST"
        assert result["fields_filled"]["attestation_name"] == record["attestation_name"]
        assert state["creates"] == state["sends"] == 1


@pytest.mark.parametrize(
    "scenario, code", [("unexpected-popup", "unexpected_surface"), ("blocked-write", "network_blocked")]
)
def test_final_safety_gate_stops_send_after_an_interruption(
    tmp_path: Path, record: dict[str, JsonValue], scenario: str, code: str
) -> None:
    with portal(record, scenario) as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED", result
        assert state["creates"] == 1 and state["sends"] == 0
        assert result["fields_filled"]["clinical.diagnosis_icd10"] == record["clinical"]["diagnosis_icd10"]
        journal = json.loads(next((Path(os.environ["XDG_STATE_HOME"]) / "prior-auth-agent").glob("*.json")).read_text())
        assert journal["phase"] == "DRAFT"
        events = (tmp_path / "result.actions.jsonl").read_text()
        assert code in events and "send_intent_persisted" not in events


@pytest.mark.parametrize(
    "scenario, field, message, phase",
    [
        ("answer-validation", "clinical.diagnosis_icd10", "Diagnosis is not accepted", "DRAFT"),
        ("send-validation", "attestation_name", "Attestation name is not accepted", "SEND_PENDING"),
    ],
)
def test_portal_validation_returns_the_visible_field_and_message(
    tmp_path: Path, record: dict[str, JsonValue], scenario: str, field: str, message: str, phase: str
) -> None:
    with portal(record, scenario) as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED", result
        assert result["escalation"]["field"] == field
        assert message in result["escalation"]["problem"]
        assert state["sends"] == 0
        events = (tmp_path / "result.actions.jsonl").read_text()
        assert message not in events
        journal = json.loads(next((Path(os.environ["XDG_STATE_HOME"]) / "prior-auth-agent").glob("*.json")).read_text())
        assert journal["phase"] == phase


def test_browser_interruption_keeps_verified_fields_and_explicit_resume_sends_the_same_draft(
    tmp_path: Path, record: dict[str, JsonValue], monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(workflow: RequestWorkflow) -> None:
        raise BrowserError("SENSITIVE_BROWSER_DETAIL")

    with portal(record) as (url, state):
        args = arguments(tmp_path, record, url)
        with monkeypatch.context() as patched:
            patched.setattr(RequestWorkflow, "send", interrupted)
            assert main(args) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED"
        assert result["fields_filled"]["clinical.diagnosis_icd10"] == record["clinical"]["diagnosis_icd10"]
        assert "SENSITIVE_BROWSER_DETAIL" not in json.dumps(result)
        assert state["creates"] == 1 and state["sends"] == 0
        assert main(args) == 0
        assert state["creates"] == 1 and state["sends"] == 0
        assert main(args + ["--resume-request", "OTHER"]) == 0
        assert state["creates"] == 1 and state["sends"] == 0
        assert main(args + ["--resume-request", "REQ-TEST"]) == 0
        completed = json.loads((tmp_path / "result.json").read_text())
        assert completed["status"] == "SUCCESS", completed
        assert state["creates"] == state["sends"] == 1


def test_no_patient_match_enters_exact_source_facts_and_completes_once(
    tmp_path: Path, record: dict[str, JsonValue]
) -> None:
    with portal(record, "new-patient") as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "SUCCESS", result
        assert state["creates"] == state["sends"] == 1
        expected = {**record["patient"], "gender": "Female"}
        del expected["sex"]
        assert state["wizard"]["new_patient"] == expected


def test_changed_new_patient_value_escalates_before_create(tmp_path: Path, record: dict[str, JsonValue]) -> None:
    with portal(record, "new-patient-mismatch") as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED", result
        assert result["escalation"]["field"] == "patient.zip_code"
        assert state["creates"] == state["sends"] == 0


def test_ambiguous_patient_search_never_enters_a_new_patient_or_creates_a_request(
    tmp_path: Path, record: dict[str, JsonValue]
) -> None:
    with portal(record, "ambiguous-patient") as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED"
        assert result["escalation"]["field"] == "patient.member_id"
        assert state["creates"] == state["sends"] == 0
        assert "portal.new_patient" not in (tmp_path / "result.actions.jsonl").read_text()


@pytest.mark.parametrize(
    "scenario",
    [
        "delayed-search",
        "throttled-search",
        "history-throttled",
        "history-countdown",
        "known-overlay",
        "alternate-layout",
    ],
)
def test_reviewed_variations_complete_without_repeating_writes(
    tmp_path: Path, record: dict[str, JsonValue], scenario: str
) -> None:
    with portal(record, scenario) as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "SUCCESS", result
        assert state["creates"] == state["sends"] == 1
        events = (tmp_path / "result.actions.jsonl").read_text()
        if scenario == "throttled-search":
            assert events.count('"event": "portal_read_retry"') == 2
        if scenario in ("history-throttled", "history-countdown"):
            assert events.count('"event": "history_read_retry"') == 2
        if scenario == "known-overlay":
            assert '"event": "known_overlay_dismissed"' in events
        if scenario == "delayed-search":
            assert '"binding": "portal.new_patient"' not in events


@pytest.mark.parametrize(
    "scenario", ["stale-query", "search-unavailable", "history-unavailable", "recurring-overlay", "unknown-overlay"]
)
def test_unsafe_or_exhausted_variations_escalate_before_create(
    tmp_path: Path, record: dict[str, JsonValue], scenario: str
) -> None:
    with portal(record, scenario) as (url, state):
        assert main(arguments(tmp_path, record, url)) == 0
        result = json.loads((tmp_path / "result.json").read_text())
        assert result["status"] == "ESCALATED", result
        assert state["creates"] == state["sends"] == 0
        if scenario == "search-unavailable":
            assert state["search_reads"] == 3
        if scenario == "recurring-overlay":
            assert '"code": "overlay_limit"' in (tmp_path / "result.actions.jsonl").read_text()


def test_send_throttling_is_never_retried_even_on_a_new_invocation(
    tmp_path: Path, record: dict[str, JsonValue]
) -> None:
    with portal(record, "send-throttled") as (url, state):
        args = arguments(tmp_path, record, url)
        assert main(args) == 0
        assert state["send_attempts"] == 1 and state["sends"] == 0
        assert json.loads((tmp_path / "result.json").read_text())["status"] == "ESCALATED"
        assert main(args) == 0
        assert state["send_attempts"] == 1 and state["sends"] == 0
