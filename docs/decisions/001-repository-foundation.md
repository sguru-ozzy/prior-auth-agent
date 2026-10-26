# PR 1: Reproducible foundation

PR: [Set up reproducible Python repository foundation](https://github.com/sguru-ozzy/prior-auth-agent/pull/1)

## Decision

Use Python 3.12.14, pinned uv/dependencies, a `src` package and the required `./run_agent` launcher.
Support module and installed console entry points. Run lint, tests and package builds in CI.

Keep local credentials, patient inputs and run artifacts outside Git. Workflow execution remains unavailable
in this milestone and exits without changing input or output files.

## Why

Reviewers need a reproducible installation and a stable CLI before browser behavior can be evaluated.
Testing an installed package catches missing resources and accidental dependence on the checkout directory.

## Tradeoff

This establishes packaging and execution conventions without completing any portal work.
The milestone cannot report a successful authorization.

## Verification

The PR recorded five passing tests, lint/format checks and source/wheel builds. A clean non-editable installation
also passed from a path containing spaces, including launcher use outside the repository.

## Next

Implement source validation, the result contract and ordered action logs before enabling portal access.
