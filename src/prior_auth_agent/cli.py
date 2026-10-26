"""Command-line entry point for validation and human handoff."""

import argparse
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
        epilog="Default: local preflight. --inspect-portal signs in and observes; request writes remain disabled.",
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
    parser.add_argument(
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
    arguments = parser.parse_args(argv)
    if not arguments.inspect_portal and (
        arguments.auth_origin
        or arguments.browser_config
        or arguments.planner != "reviewed"
        or arguments.state_dir
        or arguments.recovery_config
    ):
        parser.error("Browser options require --inspect-portal.")
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
    )
