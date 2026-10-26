# PR 5: Duplicate protection and recovery

PR: [Add duplicate checks and durable submission recovery](https://github.com/sguru-ozzy/prior-auth-agent/pull/5)

## Decision

Use a native per-case lock, private durable journal and verified request-history evidence before enabling writes.
The [short design](../design/duplicate-recovery.md) defines identity, transitions and restart behavior.

Keep state independent of `--out`. Stop on an open duplicate or unresolved write; recover only from the same request's
explicit sent status and receipt, or replay a saved verified outcome.

## Why

A lost browser response does not prove that Create or Send failed. Persisting intent first prevents a rerun
from treating an uncertain action as a fresh case.

## Tradeoff

Local files keep the implementation small, but require the same state directory across runs. The lock cannot
fence clinical staff or other hosts. The reviewed history adapter may escalate unfamiliar layouts.

Request writes remain disabled. The next workflow must use these boundaries and complete pre-submit validation.

## Verification

208 tests pass in development and a clean installed package, including process kill/restart, storage failure,
cross-case proof rejection, duplicates, receipt recovery and output-path changes.
Five complete live Portal A read-only inspections produced identical results and logs.
Pending-send receipts are simulated with browser fixtures; no live request was created or sent.

## Next

Complete one clean Portal A request through the required CLI, using the guard before any autosaving Create action
and before Send, then verify the portal outcome.
