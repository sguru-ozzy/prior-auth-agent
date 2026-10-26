# Duplicate protection and recovery

## Boundary

Before enabling Create or Send, one runner must own the case lock, have a durable journal and have verified
the relevant portal request history. Missing or ambiguous evidence returns `ESCALATED`.

## Case and storage

- Lock a case by portal/account, patient member ID and medication name. Verify DOB and package in portal details;
  bind outcomes to a hash of the complete source record.
- Store hashes of that identity and the supplied record; do not key state by `--out` or the working directory.
- Use an OS-backed, non-blocking lock shared by local runners. Keep the state directory private.
- Write state atomically and sync the journal and its directory before allowing a write.
  Private journals retain verified fields/outcomes needed for replay; action logs contain control metadata only.

## Write boundaries

`NEW → CREATE_PENDING → DRAFT → SEND_PENDING → RESOLVED`

History evidence is bound to the case and source-record hashes and must be complete and fresh.
The Create boundary includes opening or filling a page that may autosave a new request.
Persist `CREATE_PENDING` before that first action and `SEND_PENDING` before Send. Record the request ID after creation,
and resolve only from a verified receipt or explicit rejection. A timeout never authorizes a repeated write.

## Restart decisions

- A matching open portal request requires a human; do not create another.
- A verified saved outcome for the same record can be replayed without submitting.
- An unresolved Send resolves only from the same request, explicit `Sent to plan` status and one receipt.
  Other terminal states do not imply a submission outcome.
- Draft, missing, changed, ambiguous or unreadable state after a pending write remains an escalation.
- Changed source facts do not silently reuse an earlier outcome.

## Verification

Test competing processes, lock release after a crash, interrupted Create/Send, duplicate open requests,
incomplete history, changed inputs, corrupt state, failed persistence and outcome replay.
Test portal reads against synthetic browser fixtures and Portal A's visible UI.

## Limits

This lock covers local runners sharing the state directory. It cannot exclude simultaneous clinical staff or
other hosts. Portal-side idempotency would be needed for that guarantee. Request writes remain disabled in
this milestone; the next workflow implementation must use these write boundaries together with full pre-submit validation.
