"""Validate before access, optionally inspect, and publish a precise human handoff."""

import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from prior_auth_agent.agent.inspection import inspect_portal
from prior_auth_agent.agent.proposals import DEFAULT_BROWSER_SETTINGS
from prior_auth_agent.agent.questions import DEFAULT_WORKFLOW_SETTINGS
from prior_auth_agent.agent.recovery import DEFAULT_RECOVERY_SETTINGS
from prior_auth_agent.contracts import RunResult
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.storage.audit import ActionLog
from prior_auth_agent.storage.journal import default_state_dir
from prior_auth_agent.storage.jsonio import load_object, write_atomic
from prior_auth_agent.validation import (
    DEFAULT_SETTINGS,
    load_settings,
    validate_credentials,
    validate_patient,
    validate_url,
)


def action_log_path(output: Path) -> Path:
    return output.with_name(output.stem + ".actions.jsonl")


def _same_path(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    return left.exists() and right.exists() and left.samefile(right)


def run(
    portal_url: str,
    patient: Path,
    credentials: Path,
    output: Path,
    config: Path | None = None,
    reference_date: date | None = None,
    inspect: bool = False,
    auth_origins: list[str] | None = None,
    browser_config: Path | None = None,
    planner: Literal["reviewed", "model"] = "reviewed",
    state_dir: Path | None = None,
    recovery_config: Path | None = None,
    execute: bool = False,
    workflow_config: Path | None = None,
    resume_request: str | None = None,
) -> int:
    try:
        log_path = action_log_path(output)
        state_directory = (state_dir or default_state_dir()).expanduser().resolve()
        for target in (output, log_path):
            if target.resolve().is_relative_to(state_directory):
                raise OSError("Output artifacts must not overwrite recovery state.")
            if any(
                _same_path(target, source)
                for source in (
                    patient,
                    credentials,
                    config or DEFAULT_SETTINGS,
                    browser_config or DEFAULT_BROWSER_SETTINGS,
                    recovery_config or DEFAULT_RECOVERY_SETTINGS,
                    workflow_config or DEFAULT_WORKFLOW_SETTINGS,
                    Path(__file__).resolve().parents[2] / "local" / "model.env",
                    Path(__file__).resolve().parents[2] / ".env",
                    Path.cwd() / ".env",
                    Path.cwd() / "local" / "model.env",
                )
            ):
                raise OSError("Artifact path aliases an input.")
        if (inspect or execute) and any(
            source.resolve().is_relative_to(state_directory) for source in (patient, credentials)
        ):
            raise OSError("Inputs must not be stored as recovery journals.")
        if _same_path(output, log_path):
            raise OSError("Artifact paths must differ.")
    except (OSError, RuntimeError, ValueError):
        print("Unsafe or inaccessible artifact paths; inputs were left unchanged.", file=sys.stderr)
        return 2

    reference_date = reference_date or datetime.now(UTC).date()
    log: ActionLog | None = None
    try:
        log = ActionLog(log_path)
        log.event("run_started", reference_date=reference_date)
        result: RunResult | None = None
        issue: Issue | None = None
        try:
            settings = load_settings(config)
            configuration_digest = hashlib.sha256(
                json.dumps(settings.model_dump(), sort_keys=True).encode("utf-8")
            ).hexdigest()
            log.event("configuration_loaded", configuration_digest=configuration_digest)
            facts = load_object(patient, "patient", settings.max_input_bytes)
            log.event("patient_loaded")
            validate_patient(facts, settings, reference_date)
            log.event("patient_validated")
            login = validate_credentials(load_object(credentials, "credentials", settings.max_input_bytes))
            log.event("credentials_validated")
            validate_url(portal_url)
            if inspect or execute:
                outcome = inspect_portal(
                    portal_url,
                    login,
                    log,
                    auth_origins or [],
                    browser_config,
                    planner,
                    facts,
                    state_directory,
                    recovery_config,
                    execute,
                    workflow_config,
                    resume_request,
                )
                if isinstance(outcome, RunResult):
                    result = outcome
                else:
                    issue = outcome
            else:
                issue = Issue(
                    "portal.workflow",
                    "validation_only",
                    "Local validation completed; no portal access was requested.",
                    "Run without --validate-only to attempt the guarded portal workflow.",
                )
                log.event("portal_access_blocked", issue=issue)
        except InputProblem as error:
            issue = error.issue
            log.event(
                "inspection_escalated"
                if issue.field.startswith(("portal.", "model.", "browser.", "recovery.")) and (inspect or execute)
                else "validation_rejected",
                issue=issue,
            )
        if result is None:
            assert issue is not None
            result = RunResult.from_issue(issue)
        log.event("outcome_selected", issue=issue, status=result.status)
    except OSError:
        result = RunResult.from_issue(
            Issue(
                "artifacts.action_log",
                "log_unavailable",
                "A complete action log could not be written.",
                "Restore write access to the action log location, then rerun.",
            )
        )
    finally:
        if log is not None:
            try:
                log.close()
            except OSError:
                result = RunResult.from_issue(
                    Issue(
                        "artifacts.action_log",
                        "log_unavailable",
                        "The action log could not be closed safely.",
                        "Check artifact storage before rerunning.",
                    )
                )

    try:
        write_atomic(output, json.dumps(result.model_dump(), sort_keys=True, indent=2, allow_nan=False) + "\n")
    except OSError:
        print("Result could not be written; check the output location and permissions.", file=sys.stderr)
        return 2
    return 0
