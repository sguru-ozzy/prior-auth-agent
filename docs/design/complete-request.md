# Complete one prior authorization request

- Run the browser workflow with the exercise's four required CLI flags. Keep local validation and read-only inspection available explicitly.
- Validate the supplied record before portal access. Match the patient, product/package and plan identifiers without choosing between close matches.
- If the portal explicitly returns no patient matches, enter the supplied facts and verify every saved patient field before Create. Ambiguous matches still escalate.
- Keep Portal A labels, request paths, question meanings and formatting in reviewed configuration. Unknown meanings, missing facts and ambiguous matches require a human.
- Build an answer plan from the actual questionnaire, including conditional questions. Fill source facts, read them back and verify the saved answers. Leave unsupported optional answers blank.
- Persist Create intent before intake autosaves, retain the new request ID and check complete history again before Send. Never retry Create or Send after uncertain delivery.
- Check unexpected surfaces and blocked traffic before attestation and Send. Return visible portal validation messages in the private handoff; retain verified fields after browser interruptions.
- Attest only with the supplied name after final review. Verify the same request's explicit sent status and confirmation, then persist the outcome.
- Keep fixture evidence separate from live results. Portal B API/layout changes and unreviewed question meanings remain conservative escalations.

The first implementation targets one clean Portal A request. Full hard-case coverage and assignment-level evaluation follow.

## Prescriber and attestation

Validate the supplied NPI's format locally and use it unchanged if a reviewed portal question asks for it.
The inspected Portal A account response and attestation dialog do not expose the account-to-NPI relationship.
This implementation does not independently verify that relationship or invent a mapping.
Complete the documented portal attestation with the supplied full name after verifying the saved answers.

## Interrupted drafts

Default reruns escalate on unresolved drafts or existing open requests. An operator may explicitly select
`--resume-request <ID>` to continue the journal's own unsent draft. Require unchanged source data, complete
history, no other open match, exact draft identity and source-matched saved answers. Do not create another
request or resume a pending Send. Pending Send is reconciled from the same request's sent status and receipt.

## Answer evidence

Portal A displays calendar dates as MM/DD/YYYY and stores ISO dates. Both are checked against the unchanged
source date. Only reviewed treatment names support the systemic-therapy answer; unknown names never become No.
Acitretin and Cyclosporine are included in the reviewed list using the
[AAD systemic non-biologic guideline](https://www.aad.org/member/clinical-quality/guidelines/psoriasis).
This is a closed terminology mapping; no patient-specific clinical policy is inferred.

## Live evidence

A fresh Portal A instance at variance off, seed 42 completed unchanged supplied P003 through the normal four-flag
CLI, without continuation. It entered a new patient, created one request, saved source-backed answers including
the subscriber's state employee ID, attested and sent once. A separate read-only check confirmed "Sent to plan",
`QMS-00001`, one Send and a resolved journal. The action log records one Create and one Send.

Artifacts: `artifacts/fresh-p003-final.json`, its action log, `artifacts/fresh-p003-final.portal.json` and
`artifacts/fresh-p003-final.png`. They are private and ignored by Git. The earlier P002 confirmation and stopped
intake journals were preserved before the user-approved portal restart; the fresh environment uses separate state.
The 254 isolated tests pass, including five identical fixture runs. Live hard cases and unseen Portal B remain unverified.
