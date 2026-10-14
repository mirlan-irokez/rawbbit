# Changelog

## Unreleased

- Makes the browser-game source directory self-contained by keeping the protocol modules needed by the build under `vendor/rawbbit-client/`.

## 0.1.0 — initial source version

- Labels the direct-client quick start as a lower-level integration path without the adapter's host gate/durable-denial protocol. Documents copy-in adapter version ownership and recommends a maintained consent facade export as a future architectural improvement; no export or runtime behavior changed.
- Adds a separate ESM browser-game client with consent-gated IndexedDB enqueue and coordinated delivery to the existing Collector batch endpoint.
- Adds shared-session support, bounded queueing, explicit quarantine/recovery operations, lifecycle pause/reconfirmation, and an optional consumer-owned game adapter interface.
- Requires a `revocationStore` journal, with an origin-local localStorage helper, so failed IndexedDB purges block admissions, consent restoration and sends across page/client restarts while the marker remains available. A shared revocation lock covers marker write, queue purge and marker clear; browser/user storage loss limits restart guarantees.
- The adapter template requires a synchronous host permission check, a shared monotonically increasing permission-generation reader, and a denial callback that closes the shared gate and advances the generation before async persistence. Fetch captures and rechecks that generation under the host lock, fencing a delayed request from an earlier allowed period after withdrawal and re-consent. It uses the same top-level/runtime Web Locks manager precedence as the SDK, captures event timestamps and snapshots before waiting on the host lock, preserves SDK operation/byte backpressure while calls wait, and includes the initial host-lock wait in the `flush()` deadline and remaining budget. The host gate covers existing adapter instances if SDK lock acquisition times out before marker write. The host must persist denial and generation to the durable authority read after restart; immediate readback does not prove cross-tab wiring or restart durability. The raw SDK client is not exposed by the adapter template; consumers of earlier copies using `adapter.client.track`, `trackBatch`, or `confirmConsent` must migrate to the corresponding adapter wrappers.
- Adapter hook callbacks now use a synchronous reentrancy latch: tracking called from `sessionStartFactory` or `eventContextMapper` returns `invalidEvent`/`callbackReentrancy` instead of queueing behind its own admission. A cheap JavaScript string-length precheck rejects oversized event names before serialization; the complete detached snapshot is still bounded by its encoded UTF-8 byte size.
- Keeps event names, game schemas, and committed-transition capture in the consuming game.
