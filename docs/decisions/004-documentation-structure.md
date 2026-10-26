# PR 4: Documentation structure

PR: [Shorten README and add PR decision records](https://github.com/sguru-ozzy/prior-auth-agent/pull/4)

## Decision

Keep setup, common commands and current scope in the repository README.
Put additional CLI cases in `docs/testing.md` and one short decision record per PR in `docs/decisions/`.
Include the documentation in source distributions.

## Why

A new reader needs a short path to running the agent. Reviewers also need the reasons behind its boundaries
and tradeoffs. Separate decision records preserve that history as the implementation changes.

## Tradeoff

There are more files to navigate and maintain. An index and relative links make them discoverable.
Commands stay in operational guides; reviewed configuration stays in source. Decision records describe their
PR's milestone and link to later changes.

## Verification

Checked Markdown links and code fences, valid/invalid preflight CLI outcomes, evaluator help and package builds.
Verified that source archives contain documentation and exclude private local files.

## Next

Add a decision record with the next implementation PR, and update the README's scope when request writes become available.
