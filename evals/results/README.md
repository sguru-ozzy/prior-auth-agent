# Portal A evaluation

Evaluated commit: `33eae5e8a7849d042cca977e335e3a3f56a3cdcc`. Settings: variance off, seed 42, validation date
2026-10-25. The [machine-readable report](portal-a.json) contains checks, hashes and provenance.

- **51 primary runs across 49 cases: 45 met expectations; 6 did not.**
- All 27 invalid-input cases escalated before browser access.
- All three supplied clean records completed. P003 completed three fresh full workflows with identical results
  and action logs; its cached reruns created no additional request or Send.
- In three independent-client checks without local journals, the agent found the existing open request and
  escalated before Create. Each portal retained exactly one request and one Send.
- All 19 documented scenario flags were exercised with P003. A flag does not prove every branch was used;
  no attachments were uploaded. Actual open-request safety is tested separately from the named duplicate flag.

## Remaining completion gaps

- `scenario-bad-gateway`: `portal.access` / `browser_interrupted`.
- `scenario-forms-empty-fallback`: `coverage.bin` / `plan_match_ambiguous`.
- `scenario-status-bounce`: `portal.access` / `browser_interrupted`.
- `scenario-load-error-once`: `portal.access` / `browser_interrupted`.
- `scenario-throttled`: `portal.access` / `read_deadline`.
- `scenario-help-badge-covers`: `portal.workflow` / `workflow_interrupted`.

All six incomplete cases stopped before Send.
The overall evaluation exits 1 while these completion targets remain unmet. The 307 isolated tests, lint,
formatting and distribution builds pass.

## Verification scope

A separate authenticated browser checked public portal records, identities, plan and product details, Send counts,
receipts, attestations and saved-round completeness. Individual clinical answer values remain supported by the
independent fixture checks and reviewable private answer evidence. This is one seed with supplied synthetic
records; unseen Portal B remains untested.

Private inputs, logs, portal details and journals remain under
`artifacts/evaluation-submission-20261025/` and are excluded from Git. Original journals and earlier failed
experiments were preserved. The evaluator stopped its owned practice servers after each run.

Reproduce with the [CLI guide](../../docs/testing.md#assignment-evaluation), using a new output directory and
free practice-server ports. Next work should address the six failed screens while retaining exact source values,
complete duplicate checks and evidence freshness.
