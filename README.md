# Prior Auth Agent

A browser agent for the Lamar Health prior authorization exercise. It uses supplied patient facts,
checks for existing requests, and either reports a verified submission or hands the case to a human.

**Verified:** 307 isolated tests and five fresh successful Portal A workflows across the three clean records.
P003's three runs produced identical results and action logs, with independent replay and duplicate checks.
The semantic browser path is implemented; its full workflow and Portal B coverage remain unverified.

[Design and decisions](docs/README.md) · [CLI guide](docs/testing.md) · [Earlier full evaluation](evals/results/README.md)

## Setup

Use Python **3.12.14** and [uv **0.13.5**](https://docs.astral.sh/uv/getting-started/installation/#installing-a-specific-version).
Run from this repository:

```bash
uv python install
uv sync --locked
uv run --no-sync playwright install chromium
./run_agent --help
```

On Linux, install Chromium with `uv run --no-sync playwright install --with-deps chromium`.
The launcher uses `.venv`; dependencies are pinned in `uv.lock`. Portal A needs its own environment,
installed according to the supplied portal README.

For the model-assisted commands below, copy `.env.example` to the ignored `.env`, set its permissions
to `600`, and enter `OPENAI_API_KEY` in your editor. Keep your existing environment file if already configured.
If your key is in `local/model.env`, substitute that path for `.env` in the commands.

## Run

In a separate terminal, starting from the agent repository:

```bash
cd ../portal-a
.venv/bin/python -m portals --variance off --seed 42
```

If the portal is already running, reuse it. Put **one supplied patient JSON object** in `local/patient.json`;
the supplied `sample_patients.json` is an array. See [prepare a patient](docs/testing.md#prepare-a-patient).

Back in the agent repository, run Portal A with model-assisted sign-in:

```bash
uv run --env-file .env --no-sync ./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient local/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/result.json \
  --planner model

cat artifacts/result.json
cat artifacts/result.actions.jsonl
```

`uv run --env-file .env` loads the key; `--planner model` enables LLM proposals during reviewed sign-in.
Patient matching, question answers, validation and submission remain code-driven on this adapter.
Loading `.env` without `--planner model` does not enable the LLM on the reviewed Portal A path.

For the required four-argument interface without model calls, remove the `uv run --env-file .env --no-sync`
prefix and `--planner model`. See the [copyable command](docs/testing.md#portal-a-without-model-calls).

Both modes validate, sign in, check duplicates, select or enter the patient, match the medication and plan,
answer supported questions, attest with the supplied name, and send once.

Read the result's `status`:

- **SUCCESS:** the same request has a verified sent status and confirmation. This does not mean payer approval.
- **FAILED:** the portal explicitly rejected a valid submission.
- **ESCALATED:** a human must resolve invalid data, an ambiguous match, missing evidence or a portal blocker.

Exit **0** means a result was written; exit **2** means invalid usage or unavailable output storage.
The result also contains `reason`, `confirmation_id`, `fields_filled` and `escalation`.
The action log is `<output-name>.actions.jsonl`. Reusing an output path replaces both files.

## Adapters and model access

- Default `--adapter auto`: recognize the reviewed Portal A interface; otherwise use the semantic browser path.
- `--adapter reviewed`: force the Portal A adapter.
- `--adapter semantic`: force the browser path that reads controls without Portal A endpoints.
- `--validate-only`: validate locally. `--inspect-portal`: sign in and check history without request writes.

For model-assisted planning across the semantic workflow, use:

```bash
uv run --env-file .env --no-sync ./run_agent \
  --portal-url http://127.0.0.1:9463/dashboard \
  --patient local/patient.json \
  --credentials ../portal-a/examples/credentials.json \
  --out artifacts/semantic-result.json \
  --adapter semantic \
  --planner model
```

Both model-assisted commands above require a configured API key. `--planner model` makes a missing key
an explicit escalation rather than silently selecting rules. On the semantic path, the LLM chooses among
code-authorized candidates; single-candidate actions and cached plans need no model call. Code owns values,
validation, permissions and outcomes.

Without `--planner model`, the reviewed Portal A path uses rules and needs no key. The semantic path uses
an available exported key automatically; without one, it uses stable candidate selection. A completed case
can replay its saved outcome before either path calls a model. See the [CLI guide](docs/testing.md) for details.

## Test and evaluate

```bash
uv run --no-sync pytest
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv build --no-sources
```

These tests use isolated synthetic fixtures and need neither Portal A nor an API key.
The [live evaluator](docs/testing.md#assignment-evaluation) starts and stops fresh Portal A servers;
stop the development portal first. It checks portal records independently, repeatability and duplicate protection.

The earlier full evaluation met **45 of 51** run expectations; six hard cases still escalated.
The latest live regression pass covered the clean records using reviewed rules, without live model calls.
The new full semantic path remains unverified. See the linked reports for scope and limitations.

## Design and recovery

**LLM proposes → validator checks → workflow authorizes → browser executes and verifies.**

- Validate before portal access. Use supplied facts and reviewed formatting; never repair input or invent answers.
- Keep task decisions separate from portal controls. Unknown required meanings or ambiguous matches escalate.
- Verify saved answers and the request's identity before Send. Bound retries and never replay uncertain Create or Send.
- Persist a case lock and write intents. Keep credentials and full patient identifiers out of action logs.
- Use only supplied synthetic data and ordinary portal UI/APIs. Do not read portal source datasets or call
  `/__state` or `/__control`.

Recovery state defaults to `${XDG_STATE_HOME:-~/.local/state}/prior-auth-agent`. Keep it across runs;
changing `--out` does not start a new case. A completed case replays its saved result; an existing open
request escalates. Preserve unresolved journals. After resetting a practice portal, use separate state
for that new test environment while retaining the old journals.

Operator-reviewed draft continuation is available through `--resume-request` on the reviewed adapter;
it cannot repeat Create or resume pending Send. The semantic adapter requires manual continuation.
Use `./run_agent --help` for configuration overrides and `--as-of` for a fixed validation date.

See the [semantic design](docs/design/semantic-workflow.md) for the bounded vocabulary, action permissions
and model-cache limits. First uncached model choices can vary. Portal B is evaluated by Lamar and has
not been available for verification.

Inputs, keys, artifacts and local state belong in the ignored `.env`, `local/`, `artifacts/` and `.state/` paths.
