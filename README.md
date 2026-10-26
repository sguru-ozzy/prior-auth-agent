# Prior Auth Agent

A browser agent for prior authorization workflows, with validated inputs, controlled actions and human escalation.

**Current scope:** local validation plus opt-in browser inspection. Inspection signs in and observes the dashboard,
then returns `ESCALATED`. Request creation, patient field writes, attestations and Send remain disabled until
duplicate protection and recovery are implemented. The default command still runs preflight only.

[Design and implementation plan](https://docs.google.com/document/d/1S0ZwLwwnvf1CUEN2kFZnqWK1KuNc32psnmL2j5aATPA)

## Setup

Supports macOS and Linux. Use Python **3.12.14** and uv **0.13.5**.
Install [uv 0.13.5](https://docs.astral.sh/uv/getting-started/installation/#installing-a-specific-version), then run
these commands from the cloned repository:

```bash
uv python install
uv sync --locked
uv run --no-sync playwright install chromium
./run_agent --help
```

On Linux, use `playwright install --with-deps chromium` to install browser system dependencies too.
Python is pinned in `.python-version`; uv and the build backend are pinned in `pyproject.toml`.
Commit `uv.lock` and use `--locked` so setup fails if dependencies and the lockfile disagree.
The launcher uses this repository's `.venv`, including when invoked from another directory.

### Local model key

Preflight and reviewed-rule inspection need no API key. For model inspection on a fresh clone:

```bash
cp .env.example .env
chmod 600 .env
```

Set `OPENAI_API_KEY` in `.env` using your editor. Skip the copy if you already have a configured `.env`.
The template is committed; `.env`, `local/` and run artifacts are ignored. Never paste keys into patient files or logs.
Load `.env` explicitly with `uv run --env-file .env`; the launcher itself reads environment variables only.
An already exported `OPENAI_API_KEY` also works.

## CLI testing

Run these commands from the repository after setup. The credentials path assumes the supplied Portal A checkout
is a sibling directory. Pass one patient JSON object; the supplied sample file contains five records.

### Preflight

No running portal or API key is needed:

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

Expected: `ESCALATED`, field `portal.workflow`, with a message that no portal access was attempted.
The fixture is copied from the first supplied synthetic record.

### Browser inspection

Start Portal A in its own checkout with variance off, then run:

```bash
./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/inspect.json \
  --as-of 2026-10-25 \
  --inspect-portal \
  --auth-origin http://127.0.0.1:9464

cat artifacts/inspect.json
cat artifacts/inspect.actions.jsonl
```

Expected: four verified authentication actions, `inspection_complete` in the log, then `ESCALATED` with
field `portal.workflow` and a message that sign-in completed but request creation and submission are disabled.
`confirmation_id` is empty and `fields_filled` is `{}`: authentication is separate from patient answers.

The portal start origin is trusted explicitly by `--portal-url`. Portal A's separate sign-in origin must be
allowed with `--auth-origin`; additional origins are never inferred from redirects.

### Live model inspection

Use the same guarded workflow, with proposals from the model:

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
```

Expected: the same inspection handoff, with four `model_requested` events when no retries are needed.
A missing key, unsupported proposal or exhausted budget escalates without switching planners or models.

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
  --as-of 2026-10-25 \
  --inspect-portal \
  --auth-origin http://127.0.0.1:9464
```

Expected: `ESCALATED`, field `patient.member_id`, with the required format and an instruction to correct
the source record. The log ends with `validation_rejected` and `outcome_selected`; credentials, browser and model
are not accessed.

All examples exit **0** when a result was written. Inspect the JSON `status` rather than treating exit 0
as a successful submission. Invalid CLI usage or unsafe/unavailable output storage exits **2**.

Each result includes `status`, `reason`, `confirmation_id`, `fields_filled` and
`escalation: {field, problem, suggested_action}`. Ordered events appear beside it in `<name>.actions.jsonl`.
Rerunning with the same output path replaces its artifacts; use separate paths to retain history.

## Decision boundary

**LLM proposes → validator checks → workflow authorizes → browser executes and verifies.**

- Labels, roles, input types and subject context produce reviewed binding candidates. Duplicate matches and
  unknown wording never become new mappings. `First name` and `Given name` are demonstrated in isolated fixtures.
- The workflow authorizes one authentication step at a time. A model proposal cannot skip a credential read-back,
  change a source value or enable a request write.
- The model receives only the observation fingerprint and authorized binding/control references.
  Raw labels, page text, URLs, patient values and credentials are excluded.
- The executor re-observes before acting, resolves one visible enabled control by label or role, verifies the
  reviewed form destination, and reads the credential back before continuing.
- Network routing permits trusted reads and one explicitly authorized authentication POST at a time.
  Other writes, untrusted navigation and WebSockets are blocked; service workers are disabled.
  This relies on the portal respecting HTTP read semantics and does not authorize arbitrary navigation.

The reviewed-rule planner is the default for this known sign-in flow. `--planner model` exercises the bounded
proposal loop under the same gates. In this milestone the model selects among already authorized references;
it does not yet interpret clinical questions or generalize unknown portal meanings.

## Validation and configuration

The agent checks Section 4 types, formats, dates, ranges, required facts and the conditional therapy start date.
It rejects duplicate JSON keys, non-finite numbers, invalid UTF-8 and arrays in place of one record.
It preserves source values and distinguishes absent fields, null, false and zero. Extra source facts remain
unchanged and uninterpreted.

Nullable or optional facts can remain missing until a portal question requires them. Plan identity, medication
matching and the account's prescriber identity require portal evidence and remain outside this milestone.

Reviewed rules live in `src/prior_auth_agent/config/validation.json`; `--config` selects a complete reviewed replacement.
Browser bindings, authentication paths, model ID and limits live in `src/prior_auth_agent/config/browser.json`;
`--browser-config` selects a complete reviewed replacement. Freeze these with the evaluated release.

The model ID is fixed to `gpt-6-luna`, with reasoning effort `none`, one strict function call, no parallel
calls and SDK retries disabled. The [official model page](https://developers.openai.com/api/docs/models/gpt-6-luna)
currently provides no dated snapshot. Provider changes and live generation remain sources of variability;
this is not a claim of guaranteed model determinism. There is no silent fallback or runtime learning.

Inspection checks a 120-second deadline between operations, with limits of 12 action cycles, 12 model attempts,
10 seconds per browser wait and
20 seconds per model request. Retryable model transport errors get at most two logged retries, using 1/2-second
backoff and numeric Retry-After. Failed attempts count toward the total budget. Invalid semantic proposals are
never resampled. Two unchanged observations stop the loop. Authentication POSTs are never retried automatically.

DOB checks use one UTC reference date per run; `--as-of YYYY-MM-DD` freezes it during evaluation.
Logs record configuration/prompt/schema fingerprints, page fingerprints and controlled action IDs.
They exclude source values, credentials, raw model responses and timestamps. Result and log files are private.

## Checks and repeatability

```bash
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
uv build --no-sources
```

Tests cover validation, unchanged facts, strict JSON, safe artifacts, reviewed aliases, subject mismatches,
ambiguous/stale proposals, authorization, SDK requests/refusals, retry budgets and real Chromium sign-in against
isolated synthetic fixtures. Python network connections are blocked in tests; Chromium accesses only the local
fixture server. The suite needs neither Portal A nor an API key.

Run the five-repeat checks independently:

```bash
uv run --no-sync pytest \
  tests/integration/test_runner.py::test_complete_logs_and_results_repeat_identically \
  tests/integration/test_browser.py::test_five_real_browser_repeats_have_identical_logs
```

Expected: `2 passed`. These compare preflight result/log bytes and fixture-browser outcomes/log bytes.

To compare live Portal A inspection results and complete logs across five fresh browser sessions:

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
The script exits 1 if any inspection fails to complete or any bytes differ. Use `--planner reviewed` without
`--env-file` for the rule-only comparison. Keep Portal A at variance off and avoid changing its state during
comparison. Matching runs are evidence for this scenario, not a guarantee of future live-model determinism.

CI installs Chromium and the package without editable mode on Ubuntu 24.04.
Reproduce that package mode with `uv sync --locked --no-editable`; return to development with `uv sync --locked`.

## Structure and local files

The CLI and runner stay at the package entry point; related code is grouped by responsibility:

```text
src/prior_auth_agent/
  cli.py, runner.py           CLI and run orchestration
  contracts.py, errors.py     Shared contracts and handoffs
  validation.py              Source-record checks
  agent/                     Proposals, model client and inspection loop
  browser/                   Browser session and DOM observer
  config/                    Reviewed validation and browser rules
  storage/                   Input JSON, result files and action logs
tests/
  unit/                      Validation, proposals, planner and storage tests
  integration/               Browser, runner and CLI tests
  fixtures/                  Supplied synthetic records
evals/
  inspect_repeatability.py   Opt-in live inspection comparison
```

- `tests/conftest.py`: shared fixtures and the Python network guard.
- `.env.example`: committed key template; `.env`: ignored local key, loaded explicitly.
- `local/`: ignored patient inputs, credentials and local configuration.
- `artifacts/`: ignored results and action logs.
- `.state/`: reserved ignored location for future recovery state.

Use only synthetic exercise data. Keep credentials and run artifacts out of Git.
Portal A runs from its own checkout and environment; its source and internal data are not agent dependencies.
