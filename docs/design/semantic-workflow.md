# Browser workflow for unfamiliar portals

The four-argument CLI must attempt the task on an unseen interface. A Portal A adapter alone cannot
meet that requirement. The runner now selects between the existing reviewed adapter and a semantic
browser workflow before request writes begin. The semantic path reads rendered controls and labeled
details; it never calls a Portal A endpoint or assumes Portal A response JSON.

## Execution

1. Sign in using observed credential fields and their form destination. Support combined and sequential
   credential screens. Restrict destinations to the supplied portal and explicitly trusted auth origins.
2. Inspect complete, unfiltered request history. Read supported tables or labeled cards, check pagination
   counts, and open medication matches to verify member ID, DOB, medication and package. An existing
   matching open request escalates. Incomplete history never clears the duplicate check.
3. Persist Create intent before entering intake. Search using supplied values, require a response tied
   to the current query, and match observed results to source facts. New-patient entry needs a current
   negative patient search. Direct-entry forms are also supported. Do not overwrite conflicting patient data.
4. Match reviewed labels first; propose a checked canonical binding for supported unfamiliar wording.
   Read values from the record in code, apply reviewed formats, select exact supported choices and verify each operation. Missing, unsupported,
   conflicting or ambiguous answers escalate. Optional unknown checkboxes also escalate because unchecked
   does not prove that the portal will preserve an unanswered value.
5. Save supported answers, reload and verify them. Verify the saved patient, medication, plan identifiers
   and NPI from labeled review details. Require the supplied full name and an observed accuracy attestation.
6. Check history again in a separate read-only page, preserving the attestation. Persist Send intent,
   execute one Send, then reload and verify the same request's explicit status and confirmation.
   An uncertain Create or Send is never retried.

## Model boundary and determinism

Code produces source-bound action candidates. For unfamiliar fields, a model may also propose a
canonical field reference with exact quotes from observed labels, help text and section context. It
cannot supply patient values, clinical answers, selectors, URLs, permissions or an outcome.

The decision boundary is explicit:

- Reviewed aliases keep their existing deterministic path. Unknown wording uses the model only when
  configured meaning, subject and widget checks admit one possible source binding.
- Code checks the returned reference against the reviewed catalog, checks the observation fingerprint,
  and verifies that evidence quotes are present and support the required meaning, subject and units.
  Contradictory subjects, negation, incompatible widgets and unsupported time periods stop the mapping.
- Only then does code read the original supplied fact. Missing facts escalate; false and zero remain
  distinct from missing values. Existing formatting and exact-choice rules still apply.
- The same resolver checks saved detail labels and canonical history columns. Conflicting values remain
  conflicts; a model cannot declare them equivalent. Field bindings never authorize Create or Send.

The binding catalog and guards live in `config/semantic.json`. It supports wording variations for
22 canonical sources, including patient identity, coverage, prescriber, medication and selected clinical
facts. It does not infer tuberculosis results, disease severity, clinical policy or unit conversions.
Evidence quotes and lexical guards reduce risk; they do not prove arbitrary clinical equivalence.
Unsupported wording still needs human review.

An exported `OPENAI_API_KEY` enables the semantic planner. Without one, reviewed aliases and stable
candidate order remain available; unfamiliar bindings escalate. Only field metadata and canonical
references enter binding prompts. Values, credentials, URLs and arbitrary selectors are excluded;
known source strings appearing in descriptions are redacted.

Action selection and field interpretation share the run deadline, model-call limit and bounded transport
retries. Strict output rejects extra values/selectors and malformed responses. Binding proposals, including
abstentions, are cached privately against metadata, model, reasoning setting, prompt, schema and policy.
Filled values do not invalidate a wording-only interpretation; every cache replay is checked again. Current
DOM fingerprints are verified before execution, so changed subjects cannot reuse a proposed fill.
First uncached model choices can vary; caching does not guarantee determinism across empty caches.
Action logs contain controlled events and hashes rather than raw prompts, answers or model responses.

## Tradeoffs and limits

The semantic network grant allows one observed UI action to cause one same-origin POST, PUT or PATCH.
HTML forms additionally constrain the destination and method. JavaScript actions cannot be restricted
by a known endpoint before observing the portal; their grant is confined to the action window and
revoked afterward. Extra mutations, untrusted destinations and unexpected windows stop the workflow.

Meanings, statuses, aliases, bounds and evidence rules live in `config/semantic.json`. This is a bounded
semantic vocabulary, not unrestricted language understanding. Unfamiliar required meanings, unsupported
widgets, incomplete pagination or missing saved evidence still escalate. Automatic profile recognition
is heuristic; explicit adapter selection is available. Neither adapter changes after request writes start.
Operator continuation of an unfamiliar portal's old draft is deliberately not automated.

Verification covers SDK-level recorded responses, cache replay and revalidation, missing facts,
wrong subjects, unsupported units, malformed proposals and changed DOM context. Two independent synthetic
browser layouts complete sign-in, intake, source-bound questions, saved-answer verification, attestation
and one verified Send. Additional browser cases confirm safe escalation and stale-action blocking.
These tests use recorded model responses; a separate live-model smoke check exercises the installed SDK.

The browser accepts an observed native POST/Redirect/GET as transport evidence, then checks the resulting
saved state. It fills the full-name attestation only after case and saved-answer verification. Neither change
relaxes duplicate detection, durable write intents or receipt checks.

Fresh Portal A regression results and exact test counts are summarized in the repository README and the
PR decision record. Actual Portal B remains unavailable; its navigation, status labels, pagination and
saved evidence may still require adaptation. In particular, the forced semantic path on Portal A currently
stops safely when request detail does not expose a supported explicit request identifier. Normal Portal A
runs retain the reviewed adapter. Earlier full evaluation results remain tied to their recorded commit.
