# PR 8: Add repeatable evaluation and independently checked evidence

PR: https://github.com/sguru-ozzy/prior-auth-agent/pull/8

## Decision

Add an opt-in evaluation runner with reviewed case expectations, independent public-record checks and exact
full-workflow repeatability. Preserve source-bound runtime decisions and portal data. Fix the query-echo comparison and accessible-name
lookup regressions exposed by live clean cases.

## Why

The exercise requires a case report and repeatability evidence. Unit tests and a successful receipt alone do not
show that the portal received the right request once, or that the full sequence repeats from equivalent state.

## Tradeoff

Fresh practice processes take longer than fixture tests and require the default ports to be free. Oracle schemas
are Portal A-specific. Unexpected handoffs remain visible as missed completion targets. The verifier checks the
saved round's completeness; source-backed answer correctness also relies on the existing independent fixture
checks and private live evidence. See the [evaluation design](../design/evaluation.md).

## Verification

Evaluator tests reject wrong receipts, demographics, plan IDs, duplicate Sends, unsupported portal outcomes and
missing answers. A CLI integration test verifies precise validation handoffs, exact repeated outcomes/logs,
private artifact permissions and refusal to overwrite evidence. All 307 installed-package tests, lint, formatting
and builds pass.

The [live report](../../evals/results/README.md) records 51 primary runs: 45 met expectations and
6 completion targets escalated. All supplied clean records completed. Three fresh P003 workflows had
identical results/actions; cached reruns and independent clients without journals added no request or Send.
The live evaluation exposed two Portal A regressions absent from older fixtures: lowercase query echoes and
aria-labelledby question controls whose label text differs from their accessible names. Both now have regression
coverage; source values and saved identity checks remain exact.

## Next

Use failed checks to prioritize fixes without weakening expected outcomes, duplicate checks or escalation rules.
Rerun affected cases and freeze the submission with the README, one-page write-up and report.
