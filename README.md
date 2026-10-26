# Prior Auth Agent

A controlled browser agent for the Lamar Health prior authorization exercise.

**Current scope:** validate one record, sign in and observe the dashboard. Request creation, patient answers,
attestation and Send are not implemented. Successful inspection returns `ESCALATED`.

[Design and decisions](docs/README.md) · [CLI testing guide](docs/testing.md)

## Setup

Use Python **3.12.14** and [uv **0.13.5**](https://docs.astral.sh/uv/getting-started/installation/#installing-a-specific-version).

```bash
uv python install
uv sync --locked
uv run --no-sync playwright install chromium
./run_agent --help
```

On Linux, use `uv run --no-sync playwright install --with-deps chromium`.
The launcher uses the repository's `.venv`; dependencies are pinned in `uv.lock`.

For optional model testing:

```bash
[ -f .env ] || cp .env.example .env
chmod 600 .env
```

Add `OPENAI_API_KEY` to `.env` in your editor. Model commands load it explicitly with `uv run --env-file .env`.
The launcher also accepts an exported key. Keep keys and patient inputs out of Git.

## Run

Run from the repository with Portal A running separately at variance off.
The example uses a supplied synthetic fixture and assumes `portal-a` is a sibling checkout.

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

Expected: four verified sign-in actions and `inspection_complete` in the log, followed by `ESCALATED`
at `portal.workflow` because request writes are disabled. No confirmation ID or patient fields are returned.

The default command runs local validation only. Without `--inspect-portal`, omit `--auth-origin` too.
See the [testing guide](docs/testing.md) for complete preflight, model, invalid-record and repeatability commands.

Exit **0** means a result was written; read its `status`. Invalid CLI usage or unsafe/unavailable output storage exits **2**.
Results contain `status`, `reason`, `confirmation_id`, `fields_filled` and `escalation`.
Logs sit beside results as `<name>.actions.jsonl`. Reusing an output path replaces both files.

## Checks

```bash
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
uv build --no-sources
```

Tests use isolated synthetic fixtures and need neither Portal A nor an API key.
CI tests the installed package on Ubuntu. Use `uv sync --locked --no-editable` to reproduce that installation mode;
`uv sync --locked` restores editable development.

## Design

**LLM proposes → validator checks → workflow authorizes → browser executes and verifies.**

- Validate Section 4 rules before portal access. Preserve source facts, including missing, null, false and zero.
- Bind only reviewed, unique field meanings. Unknown or ambiguous meanings require a human.
- Use reviewed rules for known sign-in steps. Optional model proposals contain authorized references, never patient values,
  credentials, arbitrary selectors or URLs.
- Recheck the page and form destination, then read credentials back. Trust the portal origin and explicitly allowed
  sign-in origins; permit only reviewed authentication POSTs.
- Bound and log actions, waits and model retries. Rejected proposals are not resampled, and planners never switch silently.
- Keep private logs free of source values, credentials and raw model output. Record configuration and page fingerprints
  so runs can be compared.

[Validation rules](src/prior_auth_agent/config/validation.json) and
[browser/model settings](src/prior_auth_agent/config/browser.json) are reviewed configuration;
`--config` and `--browser-config` select complete replacements. `--as-of` fixes the DOB reference date.

Live model generation and provider updates can vary. Matching repeats provide evidence for the tested scenario;
they do not establish guaranteed determinism or unseen Portal B coverage.

## Layout

```text
src/prior_auth_agent/   CLI, contracts and validation
  agent/               Planner, proposals and workflow loop
  browser/             Browser session and DOM observer
  config/              Reviewed rules and limits
  storage/             JSON, results and action logs
tests/                 Unit/integration tests and synthetic fixtures
evals/                 Opt-in live repeatability checks
docs/                  Testing guide and PR decision records
```

`.env`, `local/`, `artifacts/` and `.state/` are ignored. Use only synthetic exercise data.
