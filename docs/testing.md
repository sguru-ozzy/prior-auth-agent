# CLI testing

Run commands from the repository after [setup](../README.md#setup).
Use only synthetic exercise data. Pass one JSON object; the supplied sample-patient file is an array.

## Local validation

No running portal or API key is needed.

```bash
./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/preflight.json \
  --as-of 2026-10-25
```

Expected: `ESCALATED` at `portal.workflow`, with no portal access attempted.
Browser inspection without a model is shown in the [README](../README.md#run).

## Model inspection

Start Portal A at variance off and configure `OPENAI_API_KEY` in the ignored `.env`.

```bash
uv run --env-file .env --no-sync ./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/inspect-model.json \
  --as-of 2026-10-25 \
  --inspect-portal \
  --auth-origin http://127.0.0.1:9464 \
  --planner model

cat artifacts/inspect-model.json
cat artifacts/inspect-model.actions.jsonl
```

Expected for a clean case: four model requests when no retries are needed, verified sign-in,
`request_history_verified` and `duplicate_check_clear`. The result is `ESCALATED` at `portal.workflow`
because request writes are disabled. Existing open requests escalate at `portal.request`.
A missing key or rejected proposal escalates without switching models or planners.

## Invalid record

Create a copy with an invalid member ID.

```bash
.venv/bin/python - <<'PYTHON'
import json
from pathlib import Path

record = json.loads(Path("tests/fixtures/patient.json").read_text(encoding="utf-8"))
record["patient"]["member_id"] = "INVALID"
Path("local").mkdir(exist_ok=True)
Path("local/invalid-patient.json").write_text(json.dumps(record), encoding="utf-8")
PYTHON

./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient local/invalid-patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/invalid.json \
  --as-of 2026-10-25 \
  --inspect-portal \
  --auth-origin http://127.0.0.1:9464
```

Expected: `ESCALATED` at `patient.member_id`, with the required format and correction.
The log records `validation_rejected` before credentials, browser or model access.

## Duplicate and recovery safeguards

```bash
uv run --no-sync pytest \
  tests/unit/test_recovery.py \
  tests/integration/test_recovery_cli.py \
  tests/integration/test_request_history.py
```

These tests cover process locks/crashes, pending Create/Send, complete history, open duplicates, receipt recovery,
changed source facts, corrupt/private state and replay to a different output path. Browser fixtures simulate
submission history; they do not create or send a request in Portal A.

For a read-only Portal A check, use the [README command](../README.md#run). Recovery uses a private user state
directory by default. Keep it across runs; changing `--out` does not start a new case.
Use `--state-dir <directory>` to select a separate synthetic test environment, not to bypass pending state.

## Repeatability

Run the five-repeat preflight and fixture-browser checks without Portal A or an API key.

```bash
uv run --no-sync pytest \
  tests/integration/test_runner.py::test_complete_logs_and_results_repeat_identically \
  tests/integration/test_browser.py::test_five_real_browser_repeats_have_identical_logs
```

Expected: `2 passed`.

For five live model inspections, keep Portal A at variance off and avoid other changes to portal state.

```bash
uv run --env-file .env --no-sync python evals/inspect_repeatability.py \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out-dir artifacts/model-repeatability \
  --auth-origin http://127.0.0.1:9464 \
  --as-of 2026-10-25 \
  --planner model \
  --repeats 5
```

Expected: `passed: true`, `all_inspections_complete: true`, `identical_results_and_logs: true`.
The evaluator requires both sign-in and verified request-history inspection. It exits **1** if either is incomplete
or results/logs differ. To test reviewed rules,
omit `--env-file .env` and change `--planner model` to `--planner reviewed`.

Identical runs are evidence for this scenario, not a guarantee of future live-model behavior.
