# Evaluation evidence

Run the required CLI against reviewed cases from the supplied synthetic records. The manifest contains explicit
invalid variations, clean records, and all 19 scenario names exposed by Portal A's documented scenario enum.
Changes belong to test inputs; the agent still receives and preserves one record unchanged.

For every live repetition, the evaluator owns a fresh `python -m portals --variance off --seed 42` process and a
separate recovery directory. It refuses occupied ports and stops only its own process. It never resets another
server, edits portal data, or calls hidden control endpoints. Existing journals remain untouched.

A separate signed-in browser reads every public request-list page before and after the run. It verifies new
request details, patient demographics, plan identifiers, medication/package, one Send, the receipt in the actual
Send record, the supplied attestation, and a saved sent question round with no missing answers. An escalation
with an actual Send fails verification. Raw portal details and logs remain private; the shareable report contains
checks, counts and hashes. This oracle verifies completion and saved-round completeness; individual clinical
answer correctness still relies on the source-bound agent tests and reviewable private answer evidence.

Fresh-environment repetitions compare exact result and action-log hashes. A separate rerun in the same environment
must replay the completed result and leave one request and one Send. A second client without a local journal
must also find the existing open request and escalate before Create; the original resolved journal is preserved. Replaying a cached result is not counted as
an independent full-workflow repetition. Validation cases run the normal CLI against an unavailable loopback port
and must reject before any browser configuration or case lock.

Every selected case keeps its expected status and field in the manifest. Unexpected escalation is a failed
completion target, even if the stop was safe. Missing oracle evidence is an evaluation error. Reports record the
commit, dirty-tree flag, input/manifest hashes, selected and unrun cases, and failed checks. No normalization hides
changing receipts, outcomes or action sequences. Evidence directories are never reused or overwritten.

The first clean smoke evaluation exposed a query-echo regression: Portal A lowercases the echoed search term.
The adapter now applies that explicit configured comparison only to the query echo, while preserving exact
source and saved-patient checks. Fixtures reproduce the observed lowercase echo, including stale-query rejection.

The same evaluation found that Portal A's question controls have correct accessible names but different label-text
content. Field lookup now accepts exact reviewed accessible names through textbox, combobox and spinbutton roles,
while retaining label support for native date inputs. The final locator still intersects with fillable input
and textarea elements and requires one target. A browser fixture reproduces aria-labelledby with hidden required
markers; broad section matches remain excluded.

The named duplicate scenario did not expose a matching open request for supplied P003 in the public record list.
Its expected outcome is therefore success for that record. Duplicate safety is separately tested with an actual
verified open request and an independent client, rather than inferred from the scenario name.
