"""Opt-in live sign-in repeatability check. Never creates or sends a request."""

import argparse
import json
from datetime import date
from pathlib import Path

from prior_auth_agent.runner import action_log_path, run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--patient", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--auth-origin", action="append", default=[])
    parser.add_argument("--planner", choices=("reviewed", "model"), default="reviewed")
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    arguments = parser.parse_args()
    if not 2 <= arguments.repeats <= 10:
        parser.error("--repeats must be between 2 and 10.")
    observed: list[tuple[bytes, bytes]] = []
    complete = True
    model_calls = 0
    for index in range(arguments.repeats):
        output = arguments.out_dir / f"run-{index + 1}.json"
        code = run(
            arguments.portal_url,
            arguments.patient,
            arguments.credentials,
            output,
            reference_date=arguments.as_of,
            inspect=True,
            auth_origins=arguments.auth_origin,
            planner=arguments.planner,
        )
        if code != 0:
            print(json.dumps({"passed": False, "problem": "Run artifacts could not be written."}))
            return 1
        log = action_log_path(output).read_bytes()
        events = [json.loads(line) for line in log.splitlines()]
        complete = complete and any(event["event"] == "inspection_complete" for event in events)
        model_calls += sum(event["event"] == "model_requested" for event in events)
        observed.append((output.read_bytes(), log))
    identical = all(pair == observed[0] for pair in observed)
    passed = complete and identical
    print(
        json.dumps(
            {
                "passed": passed,
                "runs": len(observed),
                "all_inspections_complete": complete,
                "identical_results_and_logs": identical,
                "model_calls": model_calls,
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
