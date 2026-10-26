"""The opt-in evaluator runs the real CLI and preserves shareable versus private artifacts."""

import json
import stat
import subprocess
import sys
from pathlib import Path

from pydantic import JsonValue

ROOT = Path(__file__).resolve().parents[2]


def test_evaluation_cli_checks_validation_and_identical_runs_without_network(
    tmp_path: Path, record: dict[str, JsonValue]
) -> None:
    manifest = json.loads((ROOT / "evals" / "portal_a.json").read_text())
    manifest["cases"] = [
        case for case in manifest["cases"] if case["id"] in ("invalid-member", "required-therapy-date")
    ]
    manifest["cases"][0]["repeats"] = 3
    patient = tmp_path / "records.json"
    patient.write_text(json.dumps([record]))
    credentials = tmp_path / "credentials.json"
    credentials.write_text('{"username":"SYNTHETIC_PRIVATE_USER","password":"SYNTHETIC_PRIVATE_PASSWORD"}')
    cases = tmp_path / "manifest.json"
    cases.write_text(json.dumps(manifest))
    output = tmp_path / "results"
    command = [
        sys.executable,
        str(ROOT / "evals" / "run_suite.py"),
        "--manifest",
        str(cases),
        "--records",
        str(patient),
        "--credentials",
        str(credentials),
        "--out-dir",
        str(output),
        "--agent-root",
        str(ROOT),
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["passed"] and report["summary"] == {"runs": 4, "passed": 4, "failed": 0}
    assert report["repeatability"]["invalid-member"]["identical_results_and_actions"]
    assert all(run["checks"]["validation_before_portal"] for run in report["runs"])
    assert not any(run["portal_checked"] for run in report["runs"])
    assert stat.S_IMODE((output / "report.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "invalid-member" / "1" / "patient.json").stat().st_mode) == 0o600
    shareable = (output / "report.json").read_text() + completed.stdout + completed.stderr
    assert "SYNTHETIC_PRIVATE_USER" not in shareable and "SYNTHETIC_PRIVATE_PASSWORD" not in shareable
    assert record["patient"]["member_id"] not in shareable
    # An old evidence/recovery directory is never silently overwritten.
    again = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert again.returncode == 2
