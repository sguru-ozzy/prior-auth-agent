# Prior Auth Agent

A browser agent for prior authorization workflows, with validated inputs, controlled actions and human escalation.

**Current scope:** local validation, result JSON and action logs. Invalid records receive an actionable
`ESCALATED` result. Valid records also receive `ESCALATED`, with `portal.workflow` identifying that browser execution
is not implemented yet. This milestone makes no portal or model calls.

[Design and implementation plan](https://docs.google.com/document/d/1S0ZwLwwnvf1CUEN2kFZnqWK1KuNc32psnmL2j5aATPA)

## Setup

Supports macOS and Linux. Use Python **3.12.14** and uv **0.13.5**.
Install [uv 0.13.5](https://docs.astral.sh/uv/getting-started/installation/#installing-a-specific-version), then run
these commands from the cloned repository:

```bash
uv python install
uv sync --locked
./run_agent --help
```

Python is pinned in `.python-version`; uv and the build backend are pinned in `pyproject.toml`.
Commit `uv.lock` and use `--locked` so setup fails if dependencies and the lockfile disagree.
The launcher uses this repository's `.venv`, including when invoked from another directory.

## CLI testing

Run these commands from the repository after setup. No running portal or API key is needed for PR 2.
The credentials path assumes the supplied Portal A checkout is a sibling directory. Pass one patient JSON
object; the supplied sample file contains five records and cannot be passed directly.

### Valid record

This fixture is copied from the first supplied synthetic record:

```bash
./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/valid.json \
  --as-of 2026-10-25

cat artifacts/valid.json
cat artifacts/valid.actions.jsonl
```

Expected: `status: ESCALATED`, `escalation.field: portal.workflow`, empty `confirmation_id` and `fields_filled`.
The record passes local validation; browser execution is the remaining milestone.

### Invalid record

Create an intentionally invalid copy; the supplied fixture stays unchanged:

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
  --as-of 2026-10-25

cat artifacts/invalid.json
cat artifacts/invalid.actions.jsonl
```

Expected: `status: ESCALATED`, `escalation.field: patient.member_id`, with the required member ID format
and an instruction to correct the source record. The log ends with `validation_rejected` and `outcome_selected`;
credentials and the portal are not accessed.

Both examples exit **0** because a result was written. Inspect the JSON `status` rather than treating exit 0
as a successful submission. Invalid CLI usage or unsafe/unavailable output storage exits **2**.

Each written result includes `status`, `reason`, `confirmation_id`, `fields_filled` and
`escalation: {field, problem, suggested_action}`. The adjacent `<name>.actions.jsonl` records ordered events.
Rerunning with the same output path replaces its artifacts; use separate paths to retain history.

## Validation and configuration

The agent checks Section 4 types, formats, dates, ranges, required facts and the conditional therapy start date.
It rejects duplicate JSON keys, non-finite numbers, invalid UTF-8 and arrays in place of one record.
It preserves source values and distinguishes absent fields, null, false and zero. Additional source facts are
preserved without interpreting them.

Nullable or optional facts can remain missing until a portal question requires them. Plan identity, medication
matching and the account's prescriber identity require portal evidence and are deferred to the browser workflow.
Local validation alone does not establish that a record is ready to send.

Reviewed exercise patterns, allowed values and ranges live in `src/prior_auth_agent/settings.json`.
Use `--config <settings.json>` to select a complete reviewed configuration. Freeze configuration with the
evaluated release; correct bad source records rather than changing rules to make them pass.

DOB validation uses one UTC reference date per run. Use `--as-of YYYY-MM-DD` to freeze that date during evaluation.
Logs include the reference date and configuration fingerprint, while excluding source values and credentials.
Result and log files are private to the current user.

## Checks

```bash
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
uv build --no-sources
```

Run just the five-repeat preflight comparison:

```bash
uv run --no-sync pytest tests/test_runner.py::test_complete_logs_and_results_repeat_identically
```

Expected: `1 passed`. The test compares complete result and action-log bytes across five runs with a fixed reference date.

Tests cover validation boundaries, unchanged facts, strict JSON, precise handoffs, redaction, protected input paths,
storage failures and five identical preflight repeats. In-process tests forbid network connections.
These preflight checks do not establish live-model determinism or Portal A completion.

CI installs the package without editable mode and runs the checks on Ubuntu 24.04.
To reproduce its installation mode, run `uv sync --locked --no-editable` before the checks.
Return to editable development with `uv sync --locked`.

## Structure and local files

- `src/prior_auth_agent/`: typed contracts, validation, preflight runner and artifact handling.
- `tests/`: tests and supplied synthetic fixtures.
- `local/`: ignored patient inputs, credentials and local configuration.
- `artifacts/`: ignored results, logs, traces and screenshots.
- `.state/`: reserved ignored location for future recovery state.

Use only synthetic exercise data. Keep credentials and run artifacts out of Git.
Portal A runs from its own checkout and environment; its source and internal data are not agent dependencies.
