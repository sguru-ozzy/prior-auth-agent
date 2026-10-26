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
4. Bind visible fields and questions to reviewed canonical meanings. Read values from the record in code,
   apply reviewed formats, select exact supported choices and verify each operation. Missing, unsupported,
   conflicting or ambiguous answers escalate. Optional unknown checkboxes also escalate because unchecked
   does not prove that the portal will preserve an unanswered value.
5. Save supported answers, reload and verify them. Verify the saved patient, medication, plan identifiers
   and NPI from labeled review details. Require the supplied full name and an observed accuracy attestation.
6. Check history again in a separate read-only page, preserving the attestation. Persist Send intent,
   execute one Send, then reload and verify the same request's explicit status and confirmation.
   An uncertain Create or Send is never retried.

## Model boundary and determinism

Code produces source-bound action candidates. A model may choose an authorized candidate across the
workflow; it cannot supply values, clinical answers, selectors, URLs, permissions or an outcome.
Only control descriptions, canonical source references and candidate hashes enter the prompt. Source
strings and credentials are redacted. Strict structured output and current-observation validation reject
stale or unauthorized proposals.

An exported `OPENAI_API_KEY` enables this planner on the semantic path. Without a key, candidate order
is deterministic. A single candidate needs no model call. Model decisions are cached privately against
the model, prompt, schema, configuration, observation and candidate set. First uncached model choices
can still vary; caching does not establish determinism across independent empty caches or changed pages.
Action logs contain hashes and controlled metadata, not source values or raw model responses.

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

No new test cases were added. After the user authorized regression verification, the existing **307 tests**
and **five fresh Portal A workflows** passed. P003's three runs had identical result/action hashes and passed
independent replay and duplicate checks. This caught and fixed automatic profile selection around equivalent
navigation links. These checks exercise Portal A's default adapter; full execution of the forced semantic path
and actual Portal B completion remain unverified. The earlier full evaluation remains tied to its recorded commit.
