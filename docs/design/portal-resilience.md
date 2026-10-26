# Portal searches, interruptions and matching

## Search evidence

Wait for the UI search's successful GET with the exact reviewed origin, endpoint and query. Do not use visible
“No matches” as proof. Confirm the patient search through a separate read whose response echoes that query. Portal A lowercases its
query echo; accept only that configured comparison format. Patient source values and saved identity stay exact.
An empty result permits new-patient entry; one result must match every supplied identity field. Multiple results,
wrong query echoes and mismatches escalate. Verify the saved patient again before Create.

The extra read costs one request and depends on Portal A's query-echo contract. If a different portal lacks that
contract, implement a reviewed adapter that proves current-query completion before enabling new-patient entry.

## Bounded recovery

Configuration allows three attempts for read transport errors and HTTP 429/502/503/504. Retry-After delays up to 30 seconds
are respected within the run deadline; longer or invalid delays escalate. Read-only request history can use its
reviewed Try again control twice, including the observed countdown throttling screen. Wait for slow detail fields, and require complete, fresh duplicate evidence.
Create, answer-save and Send writes are never replayed by these retries. Pending writes retain the existing
journal and recovery rules.

Dismiss only a configured notice with matching full visible text, a unique dismissal button and no form fields.
A recurring notice stops after three dismissals. Unknown dialogs, windows and changed notice text require a
human. Logs contain rule IDs, retry counts and status codes, without patient values or raw transport errors.

## Portal adaptation

Use anchored, case-insensitive accessible names with reviewed aliases and flexible whitespace. Scope patient
fields to the patient section; require a unique fillable control. Support native date/number inputs and native
or custom demographic selects. Bind equivalent question wording only to an existing reviewed source and kind.
No substring guesses, inferred clinical facts or runtime model-created meanings are accepted.

Paths, labels, aliases, question meanings and limits live in reviewed JSON configuration. History markup and
response schemas remain adapter-specific. Alternate-layout fixtures verify these improvements; they do not
establish coverage of the unseen Portal B.

A live throttled history scan exercised three retries across reads and stopped before exceeding the run budget,
with no request writes. Sustained throttling or a long scan can still exceed the run or evidence-freshness budget;
those cases escalate. Reduce required history reads before considering wider budgets, and retain complete duplicate
checks. The [current evaluation](../../evals/results/README.md) records live scenario outcomes and remaining completion gaps.
