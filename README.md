# Prior Auth Agent

A browser agent for prior authorization workflows, with validated inputs, controlled actions and human escalation.

**Current scope:** repository foundation. CLI help and packaging are available. Workflow execution currently exits
with code 2 and leaves input and output files untouched. Patient validation and portal automation follow in later PRs.

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

The Python pin is in `.python-version`; the uv and build-tool pins are in `pyproject.toml`.
Commit `uv.lock` and use `--locked` so setup fails if dependencies and the lockfile disagree.
The launcher uses this repository's `.venv`, including when invoked from another directory.

The required interface is reserved for the upcoming workflow implementation:

```bash
./run_agent --portal-url <URL> --patient <patient.json> \
  --credentials <credentials.json> --out <result.json>
```

No API key or running portal is needed for the foundation checks. Model access, model selection and browser
installation will be verified when browser observation and the LLM loop are implemented.

## Checks

```bash
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
uv build --no-sources
```

CI installs the package without editable mode and runs these checks on Ubuntu 24.04. The launcher tests exercise
the repository script, installed console command and module from outside the checkout. They also verify that the
unfinished workflow preserves input and output files and that a missing environment gives setup instructions.

To reproduce CI's installation mode locally, run `uv sync --locked --no-editable` before the checks.
Return to editable development with `uv sync --locked`.

## Structure and local files

- `src/prior_auth_agent/`: installed application package and CLI.
- `tests/`: test files and synthetic fixtures; keep tests here.
- `.github/workflows/ci.yml`: installation, lint, formatting, launcher tests and package build.
- `local/`: ignored patient inputs, credentials and local configuration.
- `artifacts/`: ignored result files, logs, traces and screenshots.
- `.state/`: reserved ignored location for local recovery state.

Use only the exercise's synthetic records. Keep credentials and run artifacts out of Git.
Store reviewed, non-secret configuration separately from local secrets when validation and workflow modules are added.

Portal A is a separate supplied application. Run it from its own checkout and environment; its source and internal
data are not dependencies of this agent. Use the browser-visible portal for agent behavior.
