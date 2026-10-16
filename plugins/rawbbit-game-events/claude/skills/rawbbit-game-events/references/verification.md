# Verification and handoff

Test the selected SDK with a mocked Collector or injected transport by default.
Do not use live keys or a deployed Collector without explicit authorization.
Capture serialized requests, not only calls into the game adapter.

## Contract and ownership

- Compare the approved event map with all changed call sites. For each committed transition, assert exactly the approved event set **per destination**, with conditional events and SDK session-start prefix only when documented. Running the integration workflow twice adds no duplicate SDK copy, host, listener, call site, catalog entry, or emission.
- Assert approved business fields, types, units, domains, privacy, frequency, and authoritative snapshot time. Test missing/edge values and a meaningful worst-case burst; reject frame/tick or uncontrolled interaction volume.
- Validate wire method/path, auth-header name without printing its value, batch shape, required envelope fields, request size/count, and JSON compatibility against the bundled Collector reference. Then separately test field ownership: attempts to supply top-level `received_at`/`ingest` or override SDK-owned session/consent must be rejected or ignored and must not appear as caller-controlled wire fields. Assert no NATS/storage lineage. Schema acceptance alone cannot prove ownership.
- A gameplay trigger should be admitted once per committed state transition, but at-least-once delivery may retry the same frozen event ID. Do not assert exactly-once Collector ingestion.

## Lifecycle and isolation

- Verify consent unknown, granted, denied, withdrawn, failed withdrawal, and restart. Once the host denial gate closes, no **new** dispatch starts; a committed purge/denial barrier prevents queued replay after restart. A request already in flight can reach the Collector and cannot be recalled. Pending/failed withdrawal remains denied until repaired.
- Verify pseudonymous identity, approved account-ID transitions if present, session renewal, reload/scene changes, offline enqueue/retry, queue limits/expiry, rejected batches/fault handling, and missing configuration according to the chosen SDK. An SDK-level acceptance result does not imply server delivery.
- In dual-write, inject a failure in each adapter independently. Gameplay and the other adapter must still work. In partial approval, only approved instrumentation changes; unapproved event proposals stay labeled proposed and existing emissions/contracts remain intact.
- For an unsupported engine or browser embedding, verify a gap report is produced **without** installing an SDK or claiming verified integration.

## Target gates

Run the game's own lint/typecheck/build/tests and the SDK's relevant tests.
Unity requires actual Editor compilation and applicable backend/device execution before a verified-target claim.
Browser checks require the actual secure top-level capability set, CSP/CORS/storage conditions, and relevant browser tests; Playwright WebKit is not an iOS device test.
Report each unrun gate explicitly.
Review the final game diff for unapproved changes and key exposure.

Handoff: changed files, implemented/deferred events, migration choice, approval evidence, SDK and copied-template revision, target support status, checks run/skipped, runtime config locations (never values), and deployment work remaining.
