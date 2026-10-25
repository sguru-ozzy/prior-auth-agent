"""Preflight orchestration; browser access remains disabled in this milestone."""

import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from prior_auth_agent.audit import ActionLog
from prior_auth_agent.contracts import RunResult
from prior_auth_agent.errors import InputProblem, Issue
from prior_auth_agent.jsonio import load_object, write_atomic
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
) -> int:
    try:
        log_path = action_log_path(output)
        for target in (output, log_path):
            if any(_same_path(target, source) for source in (patient, credentials, config or DEFAULT_SETTINGS)):
                raise OSError("Artifact path aliases an input.")
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
            validate_credentials(load_object(credentials, "credentials", settings.max_input_bytes))
            log.event("credentials_validated")
            validate_url(portal_url)
            issue = Issue(
                "portal.workflow",
                "workflow_unavailable",
                "Browser workflow is not implemented; no portal access was attempted.",
                "Complete the request manually, or rerun when the browser workflow is available.",
            )
            log.event("portal_access_blocked", issue=issue)
        except InputProblem as error:
            issue = error.issue
            log.event("validation_rejected", issue=issue)
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
