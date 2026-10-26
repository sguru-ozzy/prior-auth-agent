# PR 2: Validation and results

PR: [Validate inputs and write actionable results and action logs](https://github.com/sguru-ozzy/prior-auth-agent/pull/2)

## Decision

Validate strict JSON and Section 4 rules before portal access. Keep exercise rules in reviewed configuration.
Use typed checks without replacing the original record or applying model defaults to source facts.

Write private, atomic result files and ordered action logs. Protect input files against overwrite through direct
paths, symlinks and hard links. Record the reference date and configuration fingerprint for comparison.

Return an actionable `ESCALATED` result for invalid records. Locally valid records also escalate at
`portal.workflow` until browser execution exists.

## Why

A clean handoff is safer than repairing a record or claiming completion without portal evidence.
Preserving missing, null, false and zero prevents an absent fact from becoming an unsupported answer.

## Tradeoff

Local checks cannot establish plan, medication or prescriber matches. Those decisions require portal evidence.
This milestone intentionally makes no browser or model calls.

## Verification

The PR recorded 109 passing tests in development and a clean non-editable installation, including five
byte-identical preflight repeats, privacy checks, input protection and storage failures.

## Next

Add browser observation and constrained proposals. Keep request writes disabled until duplicate protection
and interrupted-submission recovery are in place.
