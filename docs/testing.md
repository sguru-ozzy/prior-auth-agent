# CLI testing

Run commands from the repository after [setup](../README.md#setup).
Use only synthetic exercise data. Pass one JSON object; the supplied sample-patient file is an array.

## Prepare a patient

Keep your existing `local/patient.json` if you already have one. Otherwise, this selects supplied record
P003 without changing its facts. Change the record ID only to choose another supplied record.

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path

output = Path("local/patient.json")
if output.exists():
    raise SystemExit("local/patient.json already exists; kept unchanged.")
records = json.loads(Path("../portal-a/examples/sample_patients.json").read_text(encoding="utf-8"))
record = next(item for item in records if item.get("record_id") == "P003")
output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
output.touch(mode=0o600, exist_ok=False)
output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
PY
```

## Semantic browser workflow

Start Portal A, then run from the agent repository. Configure the key in `.env` first; substitute
`local/model.env` if you already keep it there.

```bash
uv run --env-file .env --no-sync ./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient local/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/semantic-result.json \
  --adapter semantic
```

The action log should identify `portal_adapter_selected` with `action: semantic`. Without a key, omit
the `uv run --env-file .env --no-sync` prefix to use deterministic candidate selection.
This exercises the new execution path on Portal A; it does not establish Portal B coverage or guarantee completion.

Use an unused supplied clean record to exercise the browser. A locally completed case replays its saved
result, and an existing open request escalates. Preserve journals; a new state directory belongs to a
separately reset test environment, not a retry of an unresolved request.

## Local validation

No running portal or API key is needed.

```bash
./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/preflight.json \
  --validate-only \
  --as-of 2026-10-25
```

Expected: `ESCALATED` at `portal.workflow`, with no portal access attempted.
For read-only inspection, add `--inspect-portal` to the [README command](../README.md#run).

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

## Complete workflow

```bash
uv run --no-sync pytest tests/unit/test_questions.py tests/unit/test_recovery.py tests/integration/test_workflow_cli.py
```

The fixture backend independently checks Create/Send intents, saved answers, exact source values, one-time writes,
new-patient entry, field-specific validation messages, final safety checks, interrupted-draft continuation,
lost-Send recovery and five identical runs from equivalent state.

The fresh live Portal A verification used unchanged supplied P003 and the normal CLI, without a resume flag:

```bash
XDG_STATE_HOME="$PWD/artifacts/fresh-portal-20261025-final" ./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient local/p003.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/fresh-p003-final.json
```

Result: `SUCCESS`, `QMS-00001`. The log records one Create and one Send. A separate portal check confirms
"Sent to plan" and exactly one Send. Evidence is in `artifacts/fresh-p003-final.portal.json` and
`artifacts/fresh-p003-final.png`. All 307 isolated tests pass; the wheel and source distribution build.
The [current case report](../evals/results/README.md) covers 49 cases, including all documented scenario flags.

Keep one recovery directory per portal environment. This run followed a user-approved fresh practice-server
restart; original journals were preserved. Never switch state directories to bypass pending writes in the same
environment. A completed case returns its persisted result without creating or sending again.

Normal runs use the four required flags. An existing open request escalates. After operator review,
`--resume-request <ID>` may continue only the journal's own unsent draft with unchanged input and freshly
verified identity, answers and history. It cannot repeat Create or resume a pending Send.

## Search and interruption checks

```bash
uv run --no-sync pytest tests/unit/test_reads.py tests/unit/test_matching.py \
  tests/integration/test_request_history.py tests/integration/test_workflow_cli.py
```

These isolated cases cover slow and stale searches, bounded read retries, reviewed and changed notices, native
controls, equivalent wording and no Send retry after throttling. The backend independently counts writes.

For a live read-only check, add `?scenario=throttled` to Portal A's URL and use `--inspect-portal`. Inspect
`portal_read_retry`, `history_read_retry` and `request_history_verified` in the action log. Existing pending cases
can correctly end in escalation even after history is verified. Do not clear their journals or send again.

The live read-only throttled run is saved privately as `artifacts/inspect-throttled-countdown.json` and its action
log. It exercised three read retries, then returned `ESCALATED` because another wait would exceed the run budget.
There were no Create/Send intents. This verifies bounded stopping; it does not prove successful completion under
sustained throttling. Keep complete-history and evidence-freshness checks when improving this case.

## Assignment evaluation

Stop your development portal before live evaluation; the runner refuses occupied ports. It starts and stops its
own fresh practice server for each live repetition. Preserve existing recovery journals and use a new output
folder. The default manifest covers 27 validation cases, three clean records and all 19 documented scenarios.

```bash
uv run --no-sync python evals/run_suite.py \
  --records ../portal-a/examples/sample_patients.json \
  --credentials ../portal-a/examples/credentials.json \
  --portal-repo ../portal-a \
  --portal-python ../portal-a/.venv/bin/python \
  --out-dir artifacts/evaluation
```

Use repeated `--case <id>` options to select cases from `evals/portal_a.json`. Unselected cases remain listed in
the report. `clean-p003` repeats the full workflow three times from fresh environments and separately checks
cached reruns. No API key is needed. Exit 0 means all expectations, independent checks and exact comparisons
passed; 1 means failures or incomplete evidence; 2 means invalid setup. Review `report.json` and each run's
private artifacts. Portal logs can include synthetic credentials; keep the artifact directory private.

The [committed report](../evals/results/README.md) records 51 primary runs: 45 met expectations
and 6 completion targets escalated. P003 also passed three cached reruns and three independent-client
open-request checks, with one request and one Send in each environment. Its three fresh result/action logs matched
exactly. A successful scenario flag only covers the branch used by that supplied record.
