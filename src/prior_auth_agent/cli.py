"""Command-line entry point for the prior authorization agent."""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Describe the agreed interface; execution is added in the next milestone."""
    parser = argparse.ArgumentParser(
        prog="run_agent",
        description="Complete one prior authorization request using supplied patient facts.",
        epilog="Foundation milestone: --help is available; workflow execution is not implemented yet.",
    )
    parser.add_argument("--portal-url", required=True, metavar="URL", help="Portal start page.")
    parser.add_argument("--patient", required=True, metavar="patient.json", help="One supplied patient record.")
    parser.add_argument(
        "--credentials",
        required=True,
        metavar="credentials.json",
        help="JSON containing the portal username and password.",
    )
    parser.add_argument("--out", required=True, metavar="result.json", help="Destination for the result.")
    parser.parse_args(argv)
    parser.error("Workflow execution is not implemented in this foundation milestone.")
