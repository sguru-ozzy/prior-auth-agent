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
        epilog="Preflight milestone: valid records escalate until the browser workflow is implemented.",
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
    arguments = parser.parse_args(argv)
    return run(
        arguments.portal_url, arguments.patient, arguments.credentials, arguments.out, arguments.config, arguments.as_of
    )
