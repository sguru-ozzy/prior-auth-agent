# PR 7: Verify current searches and recover bounded read interruptions

PR: https://github.com/sguru-ozzy/prior-auth-agent/pull/7

## Decision

Require current-query evidence before patient selection or new-patient entry. Add bounded read retries, reviewed
notice dismissal and equivalent field/question names. Preserve single-write grants and durable recovery.

## Why

An old “No matches” display must never create a patient. Temporary read failures and familiar interface changes
should not stop an otherwise supported case. Unfamiliar meanings still need an actionable human handoff.

## Tradeoff

Patient search makes an extra verification read. Aliases are deliberately closed; Portal B may need changes to
its endpoint, schema, history or layout adapter. The model's scope is unchanged. This adds no clinical inference
and makes no claim of unseen-portal coverage. See the [implementation design](../design/portal-resilience.md).

## Verification

All 286 tests pass against a freshly rebuilt non-editable package, matching CI. Lint, formatting and distribution
builds pass. Isolated browser cases cover delayed and stale searches, bounded throttling/error recovery, known and changed
notices, native selects, alternate labels/questions, and a rejected Send that is not retried on rerun. Existing
source-preservation, recovery and repeatability checks remain in the suite. The stale validation-only runner
test now explicitly selects that mode and asserts that portal inspection is never called.

A live read-only Portal A throttling check exercised three retries, then escalated because the next delay would
exceed its run budget. It created no request-write intents. Successful completion under sustained throttling is
not established; complete-history and evidence-freshness checks remain enforced.

## Next

Run the required ten-case live evaluation with Portal A's documented scenarios. Freeze the implementation and
assess Portal B without treating alias fixtures as evidence of unseen-portal success.
