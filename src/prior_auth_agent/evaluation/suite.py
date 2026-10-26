"""Run frozen CLI cases, preserve private evidence, and publish an honest summary."""

import argparse
import hashlib
import json
import subprocess
import sys
import traceback
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from pydantic import JsonValue, ValidationError

from prior_auth_agent.contracts import RunResult
from prior_auth_agent.errors import InputProblem
from prior_auth_agent.evaluation.contracts import Case, Suite, outcome_checks, patient_for, portal_checks, safe_field
from prior_auth_agent.evaluation.portal import PORTAL_URL, FreshPortal, Observer
from prior_auth_agent.runner import action_log_path
from prior_auth_agent.storage.jsonio import load_object, write_atomic
from prior_auth_agent.validation import validate_credentials


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(
    root: Path, directory: Path, credentials: Path, case: Case, reference_date: date, replay: bool = False
) -> tuple[RunResult, list[dict[str, JsonValue]]]:
    output = directory / ("replay.json" if replay else "result.json")
    url = "http://127.0.0.1:1/dashboard" if case.local_only else PORTAL_URL
    if case.scenario:
        url += "?" + urlencode({"scenario": case.scenario})
    command = [
        str(root / "run_agent"),
        "--portal-url",
        url,
        "--patient",
        str(directory / "patient.json"),
        "--credentials",
        str(credentials),
        "--out",
        str(output),
        "--as-of",
        reference_date.isoformat(),
        "--state-dir",
        str(directory / "state"),
    ]
    capture = directory / ("replay.log" if replay else "agent.log")
    capture.touch(mode=0o600, exist_ok=False)
    with capture.open("wb") as stream:
        completed = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, timeout=150)
    if completed.returncode != 0:
        raise RuntimeError("The CLI did not write a usable outcome; inspect its private log.")
    result = RunResult.model_validate_json(output.read_text())
    events = [json.loads(line) for line in action_log_path(output).read_text().splitlines()]
    return result, events


def run_case(
    root: Path, directory: Path, credentials: Path, case: Case, facts: dict[str, JsonValue], reference_date: date
) -> dict[str, JsonValue]:
    original = sha(directory / "patient.json")
    result, events = invoke(root, directory, credentials, case, reference_date)
    checks = outcome_checks(case, result, facts)
    checks["input_file_unchanged"] = sha(directory / "patient.json") == original
    if case.local_only:
        names = [event.get("event") for event in events]
        checks["validation_before_portal"] = (
            "validation_rejected" in names
            and "patient_validated" not in names
            and "browser_configuration_loaded" not in names
            and "case_locked" not in names
        )
    record: dict[str, JsonValue] = {
        "case": case.id,
        "expected_status": case.expected_status,
        "status": result.status,
        "code": next((safe_field(str(event["code"])) for event in reversed(events) if event.get("code")), ""),
        "field": safe_field(result.escalation.field) if result.escalation.field else "",
        "checks": checks,
        "result_sha256": sha(directory / "result.json"),
        "actions_sha256": sha(action_log_path(directory / "result.json")),
        "portal_checked": False,
    }
    return record


def evaluate(arguments: argparse.Namespace) -> int:
    root = Path.cwd() if arguments.agent_root is None else arguments.agent_root.resolve()
    for name in ("manifest", "records", "credentials", "out_dir", "portal_repo"):
        setattr(arguments, name, getattr(arguments, name).resolve())
    # Preserve the venv executable path: resolving its symlink selects the base interpreter instead.
    arguments.portal_python = arguments.portal_python.absolute()
    manifest = Suite.model_validate_json(arguments.manifest.read_text())
    records = json.loads(arguments.records.read_text())
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("The supplied records file must contain an array of patient objects.")
    credentials = validate_credentials(load_object(arguments.credentials, "credentials", 1048576))
    cases = [case for case in manifest.cases if not arguments.case or case.id in arguments.case]
    if not cases or (arguments.case and set(arguments.case) - {case.id for case in cases}):
        raise ValueError("Select existing case identifiers.")
    facts = {case.id: patient_for(case, records) for case in cases}
    arguments.out_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    report: dict[str, JsonValue] = {
        "working_tree_dirty": bool(dirty),
        "selected_cases": [case.id for case in cases],
        "unrun_cases": [case.id for case in manifest.cases if case not in cases],
        "version": manifest.version,
        "commit": commit,
        "manifest_sha256": sha(arguments.manifest),
        "records_sha256": sha(arguments.records),
        "reference_date": manifest.reference_date.isoformat(),
        "variance": "off",
        "seed": manifest.seed,
        "runs": [],
        "repeatability": {},
        "passed": False,
    }
    runs: list[dict[str, JsonValue]] = []
    repeatability: dict[str, JsonValue] = {}
    for case in cases:
        signatures: list[tuple[JsonValue, JsonValue]] = []
        for repeat in range(1, case.repeats + 1):
            directory = arguments.out_dir / case.id / str(repeat)
            directory.mkdir(mode=0o700, parents=True)
            write_atomic(directory / "patient.json", json.dumps(facts[case.id], indent=2) + "\n")
            try:
                if case.local_only:
                    record = run_case(
                        root, directory, arguments.credentials, case, facts[case.id], manifest.reference_date
                    )
                else:
                    with FreshPortal(
                        arguments.portal_repo.resolve(), arguments.portal_python, directory, manifest.seed
                    ):
                        observer = Observer(credentials, directory)
                        before = observer.snapshot()
                        record = run_case(
                            root, directory, arguments.credentials, case, facts[case.id], manifest.reference_date
                        )
                        after = observer.snapshot(set(before))
                        outcome = RunResult.model_validate_json((directory / "result.json").read_text())
                        checks = record["checks"]
                        assert isinstance(checks, dict)
                        checks.update(portal_checks(outcome, facts[case.id], list(after.values()), manifest.oracle))
                        record.update({"portal_checked": True, "new_requests": len(after)})
                        if case.replay:
                            replay_dir = directory / "replay-observer"
                            replay_dir.mkdir(mode=0o700)
                            replay_result, replay_events = invoke(
                                root, directory, arguments.credentials, case, manifest.reference_date, True
                            )
                            replay_after = Observer(credentials, replay_dir).snapshot(
                                set(before) | set(after), set(after)
                            )
                            checks["replay_same_outcome"] = replay_result == outcome
                            checks["replay_uses_cached_outcome"] = any(
                                e.get("event") == "outcome_replayed" for e in replay_events
                            )
                            checks["replay_no_new_requests"] = set(replay_after) == set(after)
                            checks.update(
                                {
                                    "replay_" + key: value
                                    for key, value in portal_checks(
                                        replay_result, facts[case.id], [], manifest.oracle, list(replay_after.values())
                                    ).items()
                                }
                            )
                record["repeat"] = repeat
                check_values = record["checks"]
                assert isinstance(check_values, dict)
                record["passed"] = all(value is True for value in check_values.values())
                signatures.append((record["result_sha256"], record["actions_sha256"]))
            except Exception as error:
                write_atomic(directory / "evaluation-error.log", traceback.format_exc())
                record = {
                    "case": case.id,
                    "repeat": repeat,
                    "passed": False,
                    "status": "EVALUATION_ERROR",
                    "problem": "Run or independent evidence unavailable; inspect private artifacts.",
                    "error_type": type(error).__name__,
                }
            runs.append(record)
            if case.repeats > 1:
                repeatability[case.id] = {
                    "runs": len(signatures),
                    "identical_results_and_actions": len(signatures) == case.repeats and len(set(signatures)) == 1,
                }
            report.update({"runs": runs, "repeatability": repeatability})
            write_atomic(arguments.out_dir / "report.json", json.dumps(report, sort_keys=True, indent=2) + "\n")
            print(
                json.dumps({"case": case.id, "repeat": repeat, "status": record["status"], "passed": record["passed"]}),
                flush=True,
            )
    report["passed"] = all(run["passed"] is True for run in runs) and all(
        isinstance(value, dict) and value["identical_results_and_actions"] is True for value in repeatability.values()
    )
    report["summary"] = {
        "runs": len(runs),
        "passed": sum(run["passed"] is True for run in runs),
        "failed": sum(run["passed"] is not True for run in runs),
    }
    write_atomic(arguments.out_dir / "report.json", json.dumps(report, sort_keys=True, indent=2) + "\n")
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("evals/portal_a.json"))
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--portal-repo", type=Path, default=Path("../portal-a"))
    parser.add_argument("--portal-python", type=Path, default=Path("../portal-a/.venv/bin/python"))
    parser.add_argument("--agent-root", type=Path)
    parser.add_argument("--case", action="append", default=[])
    arguments = parser.parse_args()
    try:
        return evaluate(arguments)
    except (OSError, ValueError, ValidationError, InputProblem, subprocess.SubprocessError):
        print("Evaluation could not start; check paths, manifest and private output directory.", file=sys.stderr)
        return 2
