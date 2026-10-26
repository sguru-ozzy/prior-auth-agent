# PR 3: Guarded browser inspection

PR: [Add guarded browser inspection and model proposal loop](https://github.com/sguru-ozzy/prior-auth-agent/pull/3)

## Decision

Separate proposal validation, workflow authorization and browser execution.
Use reviewed rules for known sign-in steps; offer an explicitly selected model planner under the same gates.

The model receives only page fingerprints and authorized binding/control references.
Code resolves unique reviewed meanings, checks the current page and form destination, and verifies credentials.
Allow trusted reads and one authorized authentication POST at a time.

Bound actions, model attempts, waits and transport retries. Stop on stale or ambiguous proposals.
Inspection ends at the dashboard; request creation, answers, attestation and Send remain disabled.

Group code into agent, browser, configuration and storage packages, with unit/integration tests and a separate live evaluator.

## Why

This exercises the guarded loop before allowing actions that could create or submit a request.
The model proposes within an explicit boundary; it cannot invent values or authorize its own actions.

## Tradeoff

The model currently selects already authorized references. It does not interpret clinical questions or resolve
unknown portal meanings. Authentication paths and bindings still need review for another portal.

The configured model ID is not an immutable provider snapshot. Live generation and provider changes remain
sources of variability. Network read access also relies on the portal respecting HTTP read semantics.

## Verification

The PR recorded 160 passing tests, including installed-package and real-browser fixture checks.
Five live Portal A model inspections produced identical results and logs across 20 calls.
After reorganization, a CLI inspection matched earlier artifacts byte for byte.

These checks establish sign-in behavior for the tested scenario, not full authorization completion or Portal B coverage.

## Next

Add duplicate checks, durable recovery state and write authorization before implementing the request workflow.
