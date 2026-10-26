# PR 6: Complete the guarded prior authorization workflow

PR: https://github.com/sguru-ozzy/prior-auth-agent/pull/6

## Decision

Make the required CLI complete intake, questions, full-name attestation and one verified Send. Keep portal labels,
paths, question meanings and terminology in reviewed configuration. Enter new patient facts only after an
explicit no-match result; verify the full saved patient identity before Create.

## Why

The exercise requires an actual portal submission with source-backed answers and a receipt. The supplied P003
patient is absent from search, so new-patient entry is necessary for a clean end-to-end run.

## Tradeoff

Runtime question mappings remain conservative: unknown required meanings, unsupported options and missing facts
escalate. The model is optional for authentication proposals; it does not invent clinical answers. The inspected
portal does not expose the account-to-NPI relationship, so only NPI format is independently checked.
Only an operator-selected, journaled unsent draft can continue after interruption. Pending Send is reconciled
from its own status and receipt, never retried.

## Verification

254 isolated tests pass, including source preservation, new-patient entry, ambiguous matches, conditional questions,
visible validation feedback, popup/network stops before Send, explicit draft continuation, lost-Send recovery and
five identical fixture runs. Lint, formatting and distribution builds pass.

In a user-approved fresh Portal A environment (variance off, seed 42), unchanged supplied P003 completed the normal
CLI in one invocation. Its action log records one Create and one Send; an independent read-only portal check
confirms "Sent to plan", `QMS-00001`, one Send and a resolved journal. Original journals and earlier evidence were
preserved; see the [CLI guide](../testing.md#complete-workflow) for commands and private artifact paths.

## Next

Run the required ten-case live evaluation and hard scenarios. Extend portal-independent matching and reviewed
question coverage where evidence supports it, then assess the frozen agent on Portal B. This PR does not establish
full live repeatability or unseen-portal coverage.
