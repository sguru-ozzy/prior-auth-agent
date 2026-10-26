# 009: Add a browser workflow for unfamiliar portals

**Decision:** keep Portal A's reviewed adapter and add an endpoint-independent semantic path selected
before request writes. The assignment CLI keeps its four required arguments. Optional `--adapter` and
`--semantic-config` flags support diagnosis and reviewed configuration.

**Reason:** aliases around Portal A's API contract do not support an unseen portal with different
navigation, controls and questions. The new loop discovers observed controls and lets a constrained
planner choose among source-bound actions throughout the workflow.

**Safety boundary:** deterministic code owns meanings, values, validation, duplicate evidence,
attestation, durable write intent and outcome verification. A model never supplies clinical facts or
submission permission. Unsupported evidence stops the run. See the [design](../design/semantic-workflow.md).

**Tradeoff:** generic JavaScript UI writes need an action-scoped same-origin network grant rather than
Portal A's endpoint allowlist. Only one mutation is permitted per action; uncertain writes are not retried.
The closed semantic vocabulary intentionally sacrifices coverage when meaning cannot be established.

**Verification:** lint, formatting, static typing, Python/JavaScript syntax, configuration schemas and
package builds passed. A subsequent authorized regression run found and fixed two navigation assumptions:
Portal A can expose equivalent New request links in the sidebar and dashboard, and read-only inspection
does not require an intake link. Equivalent links are accepted only when their full trusted destinations match.
The existing suite now passes **307 tests**. All three supplied clean records complete in **five fresh live runs**;
P003's three result/action hashes match, and independent cached-replay and open-request checks pass.
No test cases were added. These live checks cover the default Portal A adapter, not the forced semantic path.
The private report is `artifacts/regression-semantic-20261025-fixed/report.json`.
Prior full evaluation results remain evidence for their recorded commit.

**Next:** exercise this path against independently varied interfaces and rerun Portal A before freezing
the submission. Lamar performs the actual unseen Portal B evaluation.
