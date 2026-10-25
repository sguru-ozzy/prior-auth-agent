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

## Run

Supply one patient JSON object. The supplied Portal A sample file is an array of five records, so it cannot be
passed directly. This example uses the test fixture copied from the first supplied synthetic record and assumes
the Portal A checkout is a sibling directory:

```bash
./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/result.json
```

The result contains `status`, `reason`, `confirmation_id`, `fields_filled` and
`escalation: {field, problem, suggested_action}`. Preflight fills no portal fields and produces no confirmation ID.

- Exit **0** means the result file was written. Inspect its `status` to determine the outcome.
- Exit **2** means CLI usage is invalid or safe result writing was impossible. Input files are preserved.
- The adjacent `artifacts/result.actions.jsonl` contains ordered, redacted events. Existing artifacts at those
  paths are replaced on rerun; choose a separate output path when retaining run history.

No API key or running portal is needed for preflight. Browser installation and model access follow in the
browser workflow milestone.

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
