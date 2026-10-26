# Prior Auth Agent

A controlled browser agent for the Lamar Health prior authorization exercise.

**Status:** all three supplied clean Portal A records complete, including three identical fresh P003 runs.
All **307 isolated tests** pass. The evaluation has **45/51 runs meeting expectations**;
6 scenarios still escalate. See the [results and limits](evals/results/README.md).

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

For optional model testing, copy `.env.example` to the ignored `.env`, set its permissions to `600` and add
`OPENAI_API_KEY` in your editor. Load it explicitly with `uv run --env-file .env`; an exported key also works.

## Run

Start Portal A separately. Pass one supplied patient object; the example assumes a sibling `portal-a` checkout.

```bash
./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient tests/fixtures/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/result.json

cat artifacts/result.json
cat artifacts/result.actions.jsonl
```

The default command validates the record, signs in, checks duplicates, selects or enters the patient, and matches the medication and plan,
answers and saves supported questions, attests with the supplied full name, and sends once.
Success requires the same request's "Sent to plan" status and confirmation number.
Missing facts, unsupported questions and ambiguous matches escalate without inventing answers.

- Add `--validate-only` for local validation without portal access.
- Add `--inspect-portal` for sign-in and read-only duplicate/recovery checks.
- Add `--planner model` for constrained model proposals during sign-in; reviewed rules are the default.
- Use `--as-of YYYY-MM-DD` to fix the DOB reference date.

Exit **0** means a result was written; read its `status`. Invalid usage or unavailable output storage exits **2**.
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
CI tests a non-editable installed package on Ubuntu. Use `uv sync --locked --no-editable --reinstall-package prior-auth-agent` to reproduce it;
`uv sync --locked` restores editable development.

For opt-in live case reports and full-workflow repeatability, see [assignment evaluation](docs/testing.md#assignment-evaluation).

## Design

**LLM proposes → validator checks → workflow authorizes → browser executes and verifies.**

- Validate Section 4 rules before portal access. Preserve source facts, including missing, null, false and zero.
- Enter a new patient only after a successful search tied to the current query proves no matches.
  Verify all saved patient fields before Create.
- Retry reads within configured limits; never replay Create or Send. Dismiss only reviewed notices.
- Match reviewed equivalent labels and native controls; escalate unfamiliar or ambiguous meanings.
- Bind reviewed question wording and kinds to source fields. Unknown required meanings or missing answers escalate.
- Evaluate conditional questions; type reviewed formats and verify both visible controls and persisted answers.
- Keep optional unsupported answers blank. Prior-therapy classification uses a closed, reviewed configuration;
  unfamiliar or absent evidence is not treated as a negative answer.
- Keep model proposals free of patient values, credentials, arbitrary selectors and URLs.
- Persist the case lock and Create/Send intents before allowing narrowly scoped browser writes.
  Reconcile uncertain Send from the same request's explicit sent status and receipt.
- Check for unexpected popups and blocked requests before attestation and again before Send.
- Verify the exact attestation before Send. Validate the supplied NPI format; the inspected Portal A account
  and attestation screens do not expose the account-to-NPI relationship, so it is not independently verified.
- Keep logs free of source values, credentials and raw model output. Results retain the supplied facts used,
  rather than substituting formatted portal values.

Reviewed [validation](src/prior_auth_agent/config/validation.json),
[browser/model](src/prior_auth_agent/config/browser.json),
[recovery](src/prior_auth_agent/config/recovery.json) and
[workflow](src/prior_auth_agent/config/workflow.json) configuration can be replaced with the corresponding
`--config`, `--browser-config`, `--recovery-config` and `--workflow-config` options.

Recovery state defaults to `${XDG_STATE_HOME:-~/.local/state}/prior-auth-agent`, independently of output paths.
`--state-dir` overrides it. Preserve the directory and unresolved journals across runs.
An existing open request escalates by default. After operator review, `--resume-request <ID>` may continue only
this case's journaled, unsent draft with unchanged input and freshly verified identity, answers and history.
It cannot repeat Create or resume a pending Send. A verified completed case replays its saved result.
After resetting a practice portal, use separate recovery state for that fresh test environment; keep the original journals.

The adapter uses Portal A's observed browser controls and read endpoints. Changed APIs, unfamiliar layouts and
unreviewed question wording can escalate on Portal B. Fixture repeatability is not evidence of unseen-portal
coverage or full Portal A hard-case coverage. A live throttled inspection exercised three bounded read retries
and escalated when the next wait would exceed the run budget, without request writes.

## Layout

```text
src/prior_auth_agent/   CLI, contracts and validation
  agent/               Planner, question decisions and recovery
  browser/             Browser session, history and workflow adapter
  config/              Reviewed rules and limits
  storage/             JSON, private journals, results and logs
tests/                 Unit/integration tests and synthetic fixtures
evals/                 Opt-in live repeatability checks
docs/                  Testing guide and PR decision records
```

`.env`, `local/`, `artifacts/` and `.state/` are ignored. Use only synthetic exercise data.
