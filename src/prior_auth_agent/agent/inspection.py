"""Bounded observe/propose/validate/authorize/execute loop with request writes disabled."""

import os
import time
from pathlib import Path
from typing import Literal

from playwright.sync_api import Error as BrowserError
from pydantic import JsonValue, ValidationError

from prior_auth_agent.agent.planner import PROMPT, ModelPlanner, Planner, ReviewedPlanner, TransportProblem
from prior_auth_agent.agent.proposals import (
    BrowserSettings,
    Candidate,
    Observation,
    Proposal,
    candidates,
    digest,
    load_browser_settings,
    stop,
    validate_proposal,
)
from prior_auth_agent.agent.recovery import RecoveryGuard, load_recovery_settings
from prior_auth_agent.browser.requests import RequestHistoryReader
from prior_auth_agent.browser.session import OBSERVE_SCRIPT, PortalBrowser
from prior_auth_agent.contracts import Credentials, RunResult
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.storage.audit import ActionLog


def authorize(observation: Observation, settings: BrowserSettings, completed: set[str]) -> list[Candidate]:
    matches = candidates(observation, settings)
    bindings = {binding.id: binding for binding in settings.bindings}
    # Authentication progression is owned by code. A model cannot skip a credential read-back.
    for purpose, prerequisite in (
        ("password", None),
        ("sign_in", "password"),
        ("username", None),
        ("continue", "username"),
    ):
        options = [
            candidate
            for candidate in matches
            if bindings[candidate.binding_id].purpose == purpose and candidate.binding_id not in completed
        ]
        if options and (
            prerequisite is None or any(bindings[binding_id].purpose == prerequisite for binding_id in completed)
        ):
            return options if len(options) == 1 else []
    return []


def inspect_portal(
    portal_url: str,
    credentials: Credentials,
    log: ActionLog,
    auth_origins: list[str],
    config_path: Path | None,
    planner_kind: Literal["reviewed", "model"],
    facts: dict[str, JsonValue],
    state_directory: Path,
    recovery_config: Path | None = None,
) -> Issue | RunResult:
    settings = load_browser_settings(config_path)
    recovery_settings = load_recovery_settings(recovery_config)
    log.event(
        "browser_configuration_loaded",
        configuration_digest=digest(
            {
                "settings": settings.model_dump(),
                "recovery": recovery_settings.model_dump(),
                "prompt": PROMPT,
                "schema": Proposal.model_json_schema(),
                "observer": OBSERVE_SCRIPT,
                "planner": planner_kind,
                "origins": sorted(auth_origins),
            }
        ),
    )
    deadline = time.monotonic() + settings.run_seconds
    model: ModelPlanner | None = None
    try:
        with RecoveryGuard(state_directory, portal_url, credentials, facts, recovery_settings, log) as guard:
            cached = guard.cached_outcome()
            if cached is not None:
                return cached
            planner: Planner = ReviewedPlanner()
            if planner_kind == "model":
                api_key = os.environ.get("OPENAI_API_KEY", "")
                if not api_key:
                    raise stop(
                        "model_key_missing",
                        "OPENAI_API_KEY is not configured.",
                        "Set the API key locally; do not put it in patient files or logs.",
                        "model.configuration",
                    )
                model = ModelPlanner(settings, api_key)
                planner = model
            with PortalBrowser(portal_url, auth_origins, settings, deadline) as browser:
                inspection_loop(browser, planner, settings, credentials, log, planner_kind == "model", deadline)
                history = RequestHistoryReader(browser, recovery_settings.history, log).read(
                    facts, guard.journal.case_key, guard.journal.request_id
                )
                log.event(
                    "request_history_verified",
                    observation=digest([request.model_dump() for request in history.requests]),
                )
                result = guard.inspect(history)
                if result is not None:
                    return result
                return Issue(
                    "portal.workflow",
                    "request_writes_disabled",
                    "Sign-in and duplicate checks completed; request creation and submission remain disabled.",
                    "Continue with the request workflow milestone, or complete the request manually.",
                )
    except (BrowserError, AssertionError, ValidationError):
        raise stop(
            "browser_interrupted",
            "The browser could not complete the reviewed inspection step.",
            "Check portal access and request history before continuing; do not repeat a pending write.",
            "portal.access",
        ) from None
    finally:
        if model is not None:
            model.close()


def inspection_loop(
    browser: PortalBrowser,
    planner: Planner,
    settings: BrowserSettings,
    credentials: Credentials,
    log: ActionLog,
    uses_model: bool,
    deadline: float,
) -> Issue:
    completed: set[str] = set()
    model_calls = 0
    previous = ""
    unchanged = 0
    for _ in range(settings.max_actions):
        if time.monotonic() >= deadline:
            raise stop("run_deadline", "The inspection time limit was reached.", "Review the portal manually.")
        observation = browser.observe()
        log.event("page_observed", observation=observation.fingerprint)
        unchanged = unchanged + 1 if observation.fingerprint == previous else 0
        previous = observation.fingerprint
        if unchanged >= 2:
            raise stop(
                "no_progress",
                "The portal did not change after two repeated observations.",
                "Review the page or sign-in error manually.",
            )
        if observation.authenticated:
            if not any(binding.id in completed and binding.purpose == "sign_in" for binding in settings.bindings):
                raise stop(
                    "authentication_unverified",
                    "A dashboard was visible without a verified sign-in sequence.",
                    "Review the authentication flow before continuing.",
                    "portal.access",
                )
            log.event("inspection_complete", observation=observation.fingerprint)
            return Issue(
                "portal.workflow",
                "request_writes_disabled",
                "Sign-in and dashboard observation completed; request creation and submission are disabled.",
                "Continue with the request workflow milestone, or complete the request manually.",
            )
        allowed = authorize(observation, settings, completed)
        proposal = None
        for attempt in range(settings.transport_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise stop("run_deadline", "The inspection time limit was reached.", "Review the portal manually.")
            if uses_model:
                if model_calls >= settings.max_model_calls:
                    raise stop(
                        "model_budget",
                        "The model call limit was reached.",
                        "Review the portal state manually.",
                        "model.proposal",
                    )
                model_calls += 1
                log.event("model_requested", attempt=attempt + 1)
            try:
                proposal = planner.propose(observation, allowed, min(settings.model_timeout_seconds, remaining))
                break
            except TransportProblem as error:
                if not error.retryable or attempt == settings.transport_retries:
                    raise stop(
                        "model_unavailable",
                        "The model could not provide a proposal within the retry limit.",
                        "Check model access and service availability.",
                        "model.proposal",
                    ) from None
                delay = max(2**attempt, error.retry_after)
                if delay >= deadline - time.monotonic():
                    raise stop(
                        "run_deadline",
                        "The model retry would exceed the inspection time limit.",
                        "Retry after service recovery.",
                        "model.proposal",
                    ) from None
                log.event("model_retry", attempt=attempt + 1)
                time.sleep(delay)
        if proposal is None:
            raise stop(
                "model_response_invalid", "No proposal was available.", "Review the model service.", "model.proposal"
            )
        try:
            # Structural/semantic validation and workflow authorization are distinct gates.
            selected = validate_proposal(proposal, observation, candidates(observation, settings))
            if selected is not None and selected not in allowed:
                raise stop(
                    "action_not_authorized",
                    "The proposed action is not allowed in the current workflow state.",
                    "Review the workflow state; no action was executed.",
                )
        except InputProblem as error:
            log.event("proposal_rejected", issue=error.issue)
            raise
        log.event("proposal_validated", action=proposal.action)
        if proposal.action == "escalate":
            raise stop(
                "human_required",
                "No reviewed action can safely continue this page.",
                "Review the sign-in fields, navigation or blocking message.",
            )
        if proposal.action == "wait":
            browser.page.wait_for_timeout(min(1000, browser.timeout()))
            log.event("page_waited")
            continue
        if selected is None:
            raise stop("invalid_proposal", "The proposal has no executable target.", "Review the proposal.")
        binding = next(binding for binding in settings.bindings if binding.id == selected.binding_id)
        log.event("action_authorized", binding=binding.id, observation=observation.fingerprint)
        browser.execute(binding, observation, selected.control_ref, credentials)
        completed.add(binding.id)
        log.event("action_verified", binding=binding.id)
    raise stop("action_budget", "The browser action limit was reached.", "Review the portal state manually.")
