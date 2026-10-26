"""Controller budgets, workflow authorization and proposal rejection precede execution."""

import json
import time
from pathlib import Path

import pytest

from prior_auth_agent.agent.inspection import inspection_loop
from prior_auth_agent.agent.planner import TransportProblem
from prior_auth_agent.agent.proposals import Candidate, Control, Observation, Proposal, load_browser_settings
from prior_auth_agent.contracts import Credentials
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.storage.audit import ActionLog


class StationaryBrowser:
    def __init__(self, label: str = "Username", section: str = "") -> None:
        self.page = self
        self.executed = 0
        self.observation = Observation(
            fingerprint="current",
            authenticated=False,
            controls=[
                Control(
                    ref="c0",
                    label=label,
                    section=section,
                    role="textbox",
                    input_type="text",
                    enabled=True,
                    has_value=False,
                )
            ],
        )

    def observe(self) -> Observation:
        return self.observation

    def execute(self, *args: object) -> None:
        self.executed += 1

    def wait_for_timeout(self, milliseconds: float) -> None:
        pass

    def timeout(self) -> float:
        return 1000


class RecordedPlanner:
    def __init__(self, proposal: Proposal | TransportProblem) -> None:
        self.proposal = proposal
        self.calls = 0

    def propose(self, observation: Observation, authorized: list[Candidate], timeout: float) -> Proposal:
        self.calls += 1
        if isinstance(self.proposal, TransportProblem):
            raise self.proposal
        return self.proposal


def run_loop(browser: StationaryBrowser, planner: RecordedPlanner, tmp_path: Path, **overrides: int) -> InputProblem:
    settings = load_browser_settings().model_copy(update=overrides)
    log = ActionLog(tmp_path / "loop.jsonl")
    try:
        with pytest.raises(InputProblem) as raised:
            inspection_loop(
                browser,
                planner,
                settings,
                Credentials(username="synthetic", password="synthetic"),
                log,
                True,
                time.monotonic() + 60,
            )
        return raised.value
    finally:
        log.close()


def test_valid_semantic_binding_still_needs_workflow_authorization(tmp_path: Path) -> None:
    browser = StationaryBrowser("Given name", "Patient")
    planner = RecordedPlanner(
        Proposal(action="bind", observation="current", binding_id="patient.first_name", control_ref="c0")
    )
    assert run_loop(browser, planner, tmp_path).issue.code == "action_not_authorized"
    assert browser.executed == 0
    assert planner.calls == 1
    assert "proposal_rejected" in (tmp_path / "loop.jsonl").read_text()


def test_rejected_proposal_is_not_resampled(tmp_path: Path) -> None:
    browser = StationaryBrowser()
    planner = RecordedPlanner(Proposal(action="bind", observation="old", binding_id="auth.username", control_ref="c0"))
    assert run_loop(browser, planner, tmp_path).issue.code == "stale_proposal"
    assert browser.executed == 0
    assert planner.calls == 1


def test_repeated_waits_stop_on_no_progress(tmp_path: Path) -> None:
    browser = StationaryBrowser()
    planner = RecordedPlanner(Proposal(action="wait", observation="current", binding_id=None, control_ref=None))
    assert run_loop(browser, planner, tmp_path).issue.code == "no_progress"
    assert planner.calls == 2
    assert browser.executed == 0


def test_retries_are_bounded_logged_and_honor_retry_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    delays = []
    monkeypatch.setattr("prior_auth_agent.agent.inspection.time.sleep", delays.append)
    planner = RecordedPlanner(TransportProblem(True, 3))
    assert run_loop(StationaryBrowser(), planner, tmp_path).issue.code == "model_unavailable"
    assert planner.calls == 3
    assert delays == [3, 3]
    events = [json.loads(line)["event"] for line in (tmp_path / "loop.jsonl").read_text().splitlines()]
    assert events.count("model_requested") == 3
    assert events.count("model_retry") == 2


def test_failed_attempts_count_against_total_model_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("prior_auth_agent.agent.inspection.time.sleep", lambda _: None)
    planner = RecordedPlanner(TransportProblem(True))
    assert run_loop(StationaryBrowser(), planner, tmp_path, max_model_calls=2).issue.code == "model_budget"
    assert planner.calls == 2


def test_retry_cannot_exceed_run_deadline(tmp_path: Path) -> None:
    planner = RecordedPlanner(TransportProblem(True, 120))
    assert run_loop(StationaryBrowser(), planner, tmp_path).issue.code == "run_deadline"
    assert planner.calls == 1


def test_action_budget_stops_waits(tmp_path: Path) -> None:
    planner = RecordedPlanner(Proposal(action="wait", observation="current", binding_id=None, control_ref=None))
    assert run_loop(StationaryBrowser(), planner, tmp_path, max_actions=1).issue.code == "action_budget"


def test_dashboard_alone_does_not_prove_authentication(tmp_path: Path) -> None:
    browser = StationaryBrowser()
    browser.observation = browser.observation.model_copy(update={"authenticated": True})
    planner = RecordedPlanner(Proposal(action="escalate", observation="current", binding_id=None, control_ref=None))
    assert run_loop(browser, planner, tmp_path).issue.code == "authentication_unverified"
    assert planner.calls == 0
    assert browser.executed == 0
