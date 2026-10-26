"""Command-line entry point for validation and human handoff."""

import argparse
import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from prior_auth_agent.runner import run


def _reference_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD for --as-of.") from None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_agent",
        description="Validate one prior authorization record and report a verified outcome or human handoff.",
        epilog=(
            "Default: complete one request. --validate-only skips browser access; "
            "--inspect-portal reads without request writes."
        ),
    )
    parser.add_argument("--portal-url", required=True, metavar="URL", help="Portal start page.")
    parser.add_argument(
        "--patient", required=True, type=Path, metavar="patient.json", help="One supplied patient record."
    )
    parser.add_argument(
        "--credentials",
        required=True,
        type=Path,
        metavar="credentials.json",
        help="JSON containing the portal username and password.",
    )
    parser.add_argument("--out", required=True, type=Path, metavar="result.json", help="Destination for the result.")
    parser.add_argument(
        "--config", type=Path, help="Reviewed validation configuration; defaults to the packaged rules."
    )
    parser.add_argument(
        "--as-of", type=_reference_date, help="Reference date for reproducible DOB checks (YYYY-MM-DD)."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true", help="Validate locally without portal access.")
    mode.add_argument(
        "--inspect-portal", action="store_true", help="Sign in and observe; never create or send a request."
    )
    parser.add_argument(
        "--auth-origin", action="append", default=[], help="Explicitly trusted additional sign-in origin."
    )
    parser.add_argument("--browser-config", type=Path, help="Complete reviewed browser and model configuration.")
    parser.add_argument(
        "--planner",
        choices=("reviewed", "model"),
        default="reviewed",
        help="Reviewed authentication rules or constrained live-model proposals.",
    )
    parser.add_argument(
        "--state-dir", type=Path, help="Private recovery directory; defaults to the user state directory."
    )
    parser.add_argument("--recovery-config", type=Path, help="Complete reviewed duplicate/recovery configuration.")
    parser.add_argument("--workflow-config", type=Path, help="Complete reviewed portal workflow configuration.")
    parser.add_argument(
        "--resume-request",
        metavar="REQUEST_ID",
        help="Explicit operator continuation of this case's journaled unsent draft; never creates a new request.",
    )
    arguments = parser.parse_args(argv)
    if arguments.resume_request is not None and (
        arguments.validate_only
        or arguments.inspect_portal
        or re.fullmatch(r"[A-Za-z0-9-]+", arguments.resume_request) is None
    ):
        parser.error("--resume-request requires the complete workflow and one valid request ID.")
    if arguments.validate_only and (
        arguments.auth_origin
        or arguments.browser_config
        or arguments.planner != "reviewed"
        or arguments.state_dir
        or arguments.recovery_config
        or arguments.workflow_config
    ):
        parser.error("Browser options cannot be used with --validate-only.")
    return run(
        arguments.portal_url,
        arguments.patient,
        arguments.credentials,
        arguments.out,
        arguments.config,
        arguments.as_of,
        arguments.inspect_portal,
        arguments.auth_origin,
        arguments.browser_config,
        arguments.planner,
        arguments.state_dir,
        arguments.recovery_config,
        not (arguments.inspect_portal or arguments.validate_only),
        arguments.workflow_config,
        arguments.resume_request,
    )
