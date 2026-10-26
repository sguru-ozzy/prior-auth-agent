# 009: Browser workflow and checked model field bindings

**Decision:** keep Portal A's reviewed adapter and add an endpoint-independent semantic workflow before
request writes begin. Both support the assignment's four required arguments. `--adapter semantic` forces
the new path; `--planner model` requires configured model access.

**Reason:** Portal A endpoints and exact label aliases cannot cover an unseen portal. The semantic loop
reads observed controls, labeled details and complete request history. For supported unfamiliar wording,
the model proposes a canonical field reference with exact UI evidence; code checks meaning, subject,
widget type, units and polarity before reading the original supplied value. Multiple possible meanings,
missing facts and unsupported questions escalate. Reviewed aliases retain their existing path.

**Safety boundary:** code owns values, validation, duplicate evidence, attestation, durable Create/Send
intent and verified outcomes. Model references and quotes confer no submission permission. Cached
interpretations are checked on every use, and current DOM evidence is checked before execution.
The catalog and semantic guards are reviewed configuration. See the [design](../design/semantic-workflow.md).

**Tradeoffs:** the catalog permits wording variations around bounded meanings; it cannot prove arbitrary
clinical equivalence. First uncached model decisions can vary. JavaScript actions need an action-scoped
same-origin network grant rather than Portal A's endpoint allowlist. One mutation is permitted per action;
uncertain Create or Send is never retried. Native form redirects still require saved-state verification.
Attestation follows verification of both the case and saved answers.

**Verification:** the isolated suite passes **350 tests**. Two independent synthetic UI variants complete
the semantic workflow through one verified Send with recorded model responses. Additional cases stop for
wrong subjects, units, changed DOM context, missing facts, malformed proposals and unsafe caches. Live
model checks accepted unfamiliar forename and explicit 28-day quantity wording, replayed a cached binding,
and abstained on less explicit cycle wording; that abstention requires a human.

All three supplied clean Portal A records complete in **five fresh live runs**. P003's three result/action
hashes match; independent cached-replay and open-request checks pass with one Create and one Send per
environment. Lint, formatting, static typing, JavaScript syntax and package builds pass. The private report
is `artifacts/regression-bindings-20261025-frozen/report.json`. Earlier full evaluations remain tied to
their recorded commits.

**Limits and next step:** Lamar performs the unavailable Portal B evaluation. Navigation, statuses,
pagination and saved request evidence remain bounded by reviewed contracts. Forced semantic execution
on Portal A still stops safely at unsupported explicit request-ID evidence; normal Portal A runs use
the reviewed adapter. Expand only from observed failing evidence, preserving the same validation and
submission gates.
