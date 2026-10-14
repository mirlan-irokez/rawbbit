# Browser-game integration

This guide covers the browser-only behavior of `@rawbbit/browser-game`. It is separate from the lower-level direct-send JavaScript client and does not alter that client's behavior.

## Source build and local installation

This directory is a self-contained source distribution. Copy the entire directory, including `vendor/rawbbit-client/`, which contains the local protocol modules used by the build. It does not import source from another Rawbbit SDK or require workspace tooling. Review changes to Collector request or event behavior against the Collector contract.

The source manifest describes the ESM package `@rawbbit/browser-game@0.1.0`. To install, test, build, and pack the SDK from this directory:

```bash
# Run inside this source directory.
cd /path/to/rawbbit-browser-game
npm ci
npm test
npm run test:types
npm run build

# Optional source-only clean-copy validation; add -- --browser for Playwright.
npm run test:source-isolation

destination="$(mktemp -d)"
npm pack --pack-destination "$destination"
GAME_DIR="/absolute/path/to/new-game"
npm install "$destination/rawbbit-browser-game-0.1.0.tgz" --prefix "$GAME_DIR"
```

`npm pack` creates a local tarball; it does not upload or publish it. For host-gated integrations, retain the consent and adapter requirements below; see the [source README](./README.md) for the lower-level quick start.

## Initialize and confirm the host's permission

**Lower-level path:** the following examples use `createBrowserGameClient()` directly. They demonstrate SDK confirmation and journal behavior, not the adapter's shared host gate, permission-generation fence, or durable host-denial callback. A permission provider and `revocationStore` alone do not establish the full host-gated withdrawal protocol, especially on lock failure before marker write. For game integrations using that protocol, follow the [host-gated adapter contract](#host-gated-adapter-contract) and do not expose direct client calls that bypass it.

Provide an HTTPS Collector URL, app-scoped ingest key, app ID, and explicit environment. `prod` and `production` are different environment values. The SDK appends `/v1/events:batch` when it is not already present; URL credentials, query strings, and fragments are rejected. Plain HTTP is only accepted when the explicit development option is enabled. The key is held in the browser runtime for requests; it is not stored with queued events or included in diagnostics, but same-origin page code can access it.

```js
import {
  createBrowserGameClient,
  createLocalStorageRevocationStore,
} from "@rawbbit/browser-game"

const client = await createBrowserGameClient({
  collectorUrl: "https://collector.example",
  apiKey: "APP_SCOPED_INGEST_KEY",
  appId: "com.example.game",
  environment: "prod",
  revocationStore: createLocalStorageRevocationStore({
    appId: "com.example.game",
    environment: "prod",
  }),
  product: "example-game",
  surface: "browser_game",
  sourceType: "game_client",
  schemaVersion: 1,
  autoFlush: true,
  onPermissionRequired() {
    // Ask the host to re-evaluate its current policy after page restoration.
  },
})

const permission = await client.confirmConsent(async () => {
  // Read the current game permission; never infer consent from missing UI.
  return await gamePrivacy.getAnalyticsStoragePermission() === true
})

if (permission.status === "confirmed") {
  const stored = await client.track({
    eventName: "round_completed",
    eventParams: { round_id: "round-17", score: 4200 },
  })
  console.log(stored.status)
}
```

New client instances start with unknown permission. A host confirmation is required before tracking or upload. `revocationStore` is required; it must persist a pending withdrawal outside the event-queue IndexedDB across page/client restarts. `createLocalStorageRevocationStore({ appId, environment })` is the provided origin-local helper and stores one opaque random marker. The SDK serializes journal calls with a scoped Web Lock. A host may provide a persistent privacy-settings service instead, but its methods must be bounded and must not re-enter the SDK. `readPending()` returns the opaque token or `null`; `markPending()` persists and returns a fresh token; `clearPending(token)` compare-clears only that token and returns `true` only after durable commit. `false` or rejection must leave the marker intact. The host remains responsible for choosing a store whose persistence fits its requirements.

`confirmConsent(permissionProvider)` checks the pending journal before calling the provider and checks it again under the shared revocation lock before granting. A marker present after restart yields `consentUnknown` / `withdrawalIncomplete`; the permission callback is not invoked. The host must retry `withdrawConsent()` to commit the queue purge and clear the exact marker before asking for a fresh grant:

```js
const client = await createBrowserGameClient({
  ...gameConfig,
  revocationStore: createLocalStorageRevocationStore({
    appId: gameConfig.appId,
    environment: gameConfig.environment,
  }),
})

const permission = await client.confirmConsent(readCurrentPermission)
if (permission.safeDiagnosticCode === "withdrawalIncomplete") {
  const repair = await client.withdrawConsent()
  if (!repair.globalWithdrawalCommitted || !repair.revocationCleared) {
    // Keep the host's durable consent state denied and retry later.
  }
}
```

## Host-gated adapter contract

[`examples/GameAnalyticsAdapter.js.template`](./examples/GameAnalyticsAdapter.js.template) is consumer-owned copy-in code, not an export of `@rawbbit/browser-game`. Record which source version you copied, review fixes when updating, and validate that all cooperating adapters use the same protocol; updating the SDK package alone does not update copied adapters. Hosts own the authoritative permission store and game event definitions. Packaging the generic consent facade as a maintained, versioned export is a recommended future architectural improvement; it is not implemented by these examples.

The consumer adapter template requires `readCurrentAnalyticsPermission()`, `isAnalyticsPermissionAllowed()`, `readAnalyticsPermissionGeneration()`, and `persistAnalyticsPermissionDenied()`. The synchronous permission check must return literal `true` only while analytics is allowed. It gates adapter event admissions and is rechecked at the actual Fetch-start boundary. The template serializes permission confirmation, event admission, withdrawal, and Fetch invocation with an origin/app/environment-scoped Web Lock. Use the same native or injected Web Locks manager for every cooperating adapter instance and for the SDK itself; `config.locks` takes precedence over `config.runtime.locks` when configured. Before queueing on this lock, tracking calls reserve against the configured `maxPendingTrackOperations` and aggregate `maxPendingTrackBytes` limits; exhausted capacity returns `busy`. Track wrapper inputs, event timestamps, and batch contents are detached synchronously before waiting for the host lock. The configured `clock` is also used during SDK initialization and startup retention pruning. The denial callback's synchronous prefix must close a shared host gate consulted by `isAnalyticsPermissionAllowed()` and every `readCurrentAnalyticsPermission()` implementation across cooperating adapters/tabs before its first `await`. It must advance the shared host permission generation in that same synchronous prefix, then durably save denial and the new generation to the authority used by those readers. `readAnalyticsPermissionGeneration()` must synchronously return the shared, monotonically increasing, non-reused safe integer, and every host permission revocation/transition to denied must advance it. The adapter captures this generation before a Fetch waits for the host lock and compares it again under the lock; a delayed request from an earlier allowed period is rejected even if the host has since explicitly re-consented. The template also verifies that the async permission reader returns `false` and the synchronous check remains closed after denial. Permission callbacks must be bounded and must not re-enter the adapter. This readback checks wiring but cannot prove cross-tab integration or survival across process restart, so both remain the host's responsibility. Keep the shared gate closed if persistence fails. The template calls the host callback synchronously before waiting for the shared lock and running SDK `withdrawConsent()`. A track/trackBatch call that passed only the initial synchronous check but is still queued is checked again under the lock and denied; only a call that passes the in-lock check and enters `client.track` or `client.trackBatch` is drained before withdrawal's purge. If purge fails, the host gate still blocks new Fetch starts and repair remains required before opt-in. `flush({ timeoutMs })` starts its deadline before waiting for the host lock, forwards only its remaining budget to the SDK, and cancels the queued lock request at expiry. This also covers SDK revocation-lock timeout before marker write. A Fetch already invoked before the gate closed cannot be recalled. Do not change the host state back to granted after an incomplete/failed withdrawal; keep it denied until queue purge and marker clear both succeed, and only then allow a separate explicit re-consent flow. If host denial persistence or verification fails, the template rejects and fails closed.

```js
const adapter = await createGameAnalyticsAdapter({
  config: gameConfig,
  // Read the same shared monotonic generation used by all adapters and tabs.
  readAnalyticsPermissionGeneration: () => hostAnalyticsConsentGate.generationNow(),
  readCurrentAnalyticsPermission: async () =>
    hostAnalyticsConsentGate.isAllowedNow()
      ? await readPersistedAnalyticsPermission()
      : false,
  // Synchronous and fail-closed; admissions and Fetch starts check this.
  isAnalyticsPermissionAllowed: () => hostAnalyticsConsentGate.isAllowedNow(),
  // Close the shared gate before the first await, then persist false to the
  // same authority used by both readers. Advance the generation synchronously
  // with closure; leave both gate and generation in place if persistence fails.
  persistAnalyticsPermissionDenied: () => {
    hostAnalyticsConsentGate.closeAndAdvanceGenerationNow()
    return saveAnalyticsPermission(false, hostAnalyticsConsentGate.generationNow())
      .then(() => hostAnalyticsConsentGate.commitDenied())
  },
})
```

Both readers must consult the same host gate: `readCurrentAnalyticsPermission()` returns false while denial is pending, and `isAnalyticsPermissionAllowed()` returns true only after a separate host opt-in. `commitDenied()` releases the temporary pending state only after durable persistence; the authoritative permission must still read false. If persistence rejects, leave the gate closed. A later opt-in must be a separate host action after the adapter reports both withdrawal completion flags as true. The adapter intentionally does not expose its raw SDK client because direct SDK calls bypass these host gates. Consumers of the earlier template must add `readAnalyticsPermissionGeneration()` and synchronously advance the shared generation when permission is withdrawn, as well as migrate `adapter.client.*` calls to the wrappers: for example, use `trackCommittedTransition(eventName, eventProperties)` instead of `client.track(...)`, `trackCommittedTransitions(events)` instead of `client.trackBatch(...)`, and `confirmCurrentPermission()` instead of `client.confirmConsent(...)`. This is a source-template migration note.

Tracking calls made while the shared gate is already closed return a denied result immediately rather than queueing a future admission. `confirmCurrentPermission()` joins an active withdrawal, retries withdrawal when the adapter has latched a repair requirement, and otherwise returns denied while the host gate is closed; it cannot grant by itself. `flush()` and recovery operations also recheck permission before starting work. If withdrawal or marker repair is needed, explicitly retry `adapter.withdrawConsent()` and require both completion flags before changing the host permission back to granted.

The marker contains no queue data, IDs, endpoint or key. The helper is not a queue fallback. LocalStorage may be unavailable, user-cleared, or evicted; it and the SDK do not guarantee physical-media durability. If persistence/read/clear fails, keep the host's authoritative consent state denied and do not reinitialize or grant as a workaround. While a marker is pending, SDK clients block admissions, grants, and fetch starts. A failed queue purge remains locally blocked and restart-blocked while its pending marker remains available. If a marker write fails, or SDK lock acquisition fails before a marker is written, restart blocking cannot be guaranteed unless a prior pending marker already exists; the SDK client remains blocked and the result exposes the failure. The adapter template additionally blocks cooperating adapters' new admissions and Fetch starts through the shared synchronous host gate, even after SDK lock timeout. A call past only the initial permission check is denied by the in-lock check; calls that pass it and enter `client.track` or `client.trackBatch` are drained before the purge attempt. This depends on every adapter using the template and consulting the same gate. Restart protection requires denial committed to the same durable authority read after restart. An immediate false readback proves neither cross-tab wiring nor restart durability.

`confirmConsent(permissionProvider)` captures the shared store/endpoint/permission fence before awaiting the provider:

- A current `true` result grants only this client instance and returns `confirmed`.
- A current `false` result performs the shared withdrawal/purge transaction and returns its result.
- A rejected, thrown, or non-boolean result leaves the instance unknown; it does not retain a prior grant and does not apply a global denial.
- If the captured state changed while the provider was pending, the result cannot grant or purge against the stale state.

After a page is restored from BFCache or lifecycle suspension, the client is unknown again and calls `onPermissionRequired()`. The host must obtain/re-evaluate its current permission and call `confirmConsent()` again before tracking or replay. The SDK supplies no consent UI or policy and does not treat an absent screen as permission.

If queue purge cannot commit, the independent journal marker remains and blocks new admissions, permission confirmation, and sending across client/page restarts while that marker remains available. Retry `withdrawConsent()` after the queue is available. `globalWithdrawalCommitted` reports the IndexedDB purge; `revocationCleared` separately reports whether the marker was removed. A failure before purge commits returns `globalWithdrawalCommitted: false`, `revocationCleared: false`, and `localDisabled: true`. If purge commits but marker clearing fails, the result has `status: "withdrawn"`, `globalWithdrawalCommitted: true`, `revocationCleared: false`, `localDisabled: true`, and `safeDiagnosticCode: "revocationClearPending"`; consent remains blocked until the marker is repaired. Journal read/write/validation failures use safe diagnostic codes and are exposed in `getDiagnostics().revocationStorageCode`. Do not recreate the client or manually clear the marker to work around incomplete withdrawal.

If the host journal cannot persist/read/clear, keep the host's own authoritative privacy state denied and do not reinitialize/regrant as a workaround. A storage system that cannot persist a marker cannot provide a restart guarantee; the client remains locally disabled and returns a safe journal failure. The provided localStorage helper is user-clearable and may be unavailable or evicted; hosts with stronger persistence requirements should provide their own journal.

Tracking wrappers capture the shared host permission generation before waiting for the host lock and compare it again after acquisition. A delayed call that did not pass the in-lock check before withdrawal is denied even if the host has re-consented by the time its lock request runs; a call already inside the lock may finish and is drained before purge.

## Events and adapter hooks

The caller owns all event names and payload properties. Capture game state at the committed transition and pass a detached JSON-compatible event to `track` or `trackBatch`. Before serializing the wrapper snapshot, the adapter rejects an event name whose JavaScript string length plus JSON string delimiters exceeds `maxPendingTrackBytes`; it then checks the complete snapshot's UTF-8 byte size. The SDK snapshots input synchronously before asynchronous work and rejects cycles, accessors, unsupported values, non-finite numbers, and out-of-bounds data rather than serializing arbitrary runtime objects.

Event inputs accept `eventName`/`event_name`, optional `eventId`/`event_id`, and optional RFC3339 `eventTimestamp`/`event_timestamp`. Generic JSON maps include `eventParams`, `userProperties`, `trafficSource`, `geo`, `user`, `device`, and `consent` (camel- and snake-case aliases where supported). The SDK supplies its scoped installation ID as the default `user.user_pseudo_id`; the caller may provide an explicitly approved pseudo-ID per event through `user.user_pseudo_id`/`user.userPseudoId`. The SDK does not automatically link accounts or identities across events, apps, origins, or storage partitions. The SDK owns app/environment, session ID, web platform, event time/ID defaults, and `consent.analytics_storage=true` after permission confirmation. Do not supply Collector-owned fields or override SDK-owned session/consent fields.

Configurable defaults are `surface: "browser_game"`, `sourceType: "game_client"`, `schemaVersion: 1`, and `device.platform: "web"`. `product`, `surface`, `sourceType`, and `schemaVersion` become event-parameter defaults; event-level parameters merge over them. `defaultDevice` is detached and merged with each event's device data.

Optional adapter callbacks are synchronous, pure, and non-reentrant:

- `sessionStartFactory(context, triggeringSnapshot)` may return a consumer-defined prefix event for a new SDK session. Without it, the SDK creates no prefix event.
- `eventContextMapper(context, eventSnapshot)` may return mappings for `eventParams`, `userProperties`, `trafficSource`, `geo`, or `device`. Use it for consumer-owned properties such as an SDK session event sequence; it cannot map `user`/identity, and SDK-owned top-level/session fields and `device.platform` remain protected.

The callbacks are synchronous, pure, non-reentrant, and side-effect free; they must use the detached snapshots/context rather than read mutable game state or perform I/O. Adapter tracking reentered from either callback resolves `invalidEvent` with `safeDiagnosticCode: "callbackReentrancy"` instead of queueing work behind the active admission. The SDK can recompute callbacks on a session revision conflict, with at most three candidate/admission attempts before returning `busy`. A new session prefix and its first triggering event are inserted atomically with the same captured event timestamp; the prefix is sequence 1 and the triggering event is sequence 2. Their optional payload and name remain game-owned.

## Storage, sessions, and limits

The database scope is the browser origin/storage partition plus `appId` and `environment`; it is shared by cooperating new-SDK tabs. Collector URL is bound in metadata but does not create a second queue. The SDK creates a namespaced installation pseudo-ID, not by reading the legacy `rb_user_pseudo_id` key. No legacy helper or session is adopted automatically.

An explicitly reviewed `legacyIdentityImport` may supply a pseudo-ID only for a newly created empty scoped database. It must include a pseudo-ID no longer than 256 characters, matching `appId` and `environment`, and a `policyVersion` of 1–64 ASCII letters, digits, `_`, `.`, or `-`; it imports neither the old queue nor session state. For example:

```js
legacyIdentityImport: {
  pseudoId: "reviewed-installation-id",
  appId: "com.example.game",
  environment: "prod",
  policyVersion: "identity-import-v1",
}
```

The import option is not automatic migration from `localStorage`; existing legacy storage is never read or cleared by the SDK. An existing scoped identity is never replaced: a mismatching import is rejected, while repeated configuration with the stored pseudo-ID is allowed.

The shared session expires after more than 30 minutes without an accepted tracking operation. Exactly 30 minutes retains it. Upload, retry, reload, and visibility changes do not refresh session activity. Cooperating tabs coordinate installation, session updates, enqueue, send, and withdrawal through IndexedDB and Web Locks.

Default operational configuration:

| Config field | Default | Purpose |
| --- | ---: | --- |
| `maxQueueBytes` | 10,485,760 bytes (10 MiB) | Logical shared pending + quarantine limit |
| `retentionDays` | 7 | Eligibility from original enqueue time |
| `sessionTimeoutMs` | 1,800,000 ms (30 minutes) | Shared session inactivity threshold |
| `maxEventsPerRequest` | 500 | Maximum events in one Collector request |
| `maxRequestBytes` | 1,048,576 bytes (1 MiB) | Maximum serialized `{events:[...]}` request body |
| `maxTrackBatchEvents` | 500 | Maximum input events in one `trackBatch` |
| `maxPendingTrackOperations` | 64 | Concurrent detached track operations |
| `maxPendingTrackBytes` | 2,097,152 bytes (2 MiB) | Aggregate detached track handoff byte budget |
| `timeoutMs` | 10,000 ms | Full request/body-read deadline |
| `maxRetries` | 2 | Retries after the initial attempt in each cycle |
| `backoffMs` | 250 ms | Base for capped exponential jittered backoff |
| `retryCooldownMs` | 60,000 ms | Cooldown after the initial attempt plus two retries |
| `flushTimeoutMs` | 15,000 ms | Default caller deadline for `flush()` |
| `lockTimeoutMs` | 3,000 ms | State-lock wait deadline; does not steal a held lock |
| `storageTimeoutMs` | 3,000 ms | IndexedDB open/transaction deadline |
| `maxResponseBytes` | 65,536 bytes (64 KiB) | Maximum response body read |
| `maxSnapshotDepth` | 32 | Maximum JSON input nesting depth |
| `autoFlush` | `true` | Start eligible upload attempts automatically |

Each stored record is accounted as its frozen UTF-8 payload plus 512 logical metadata bytes. This is SDK logical accounting, not a browser storage quota or physical-disk guarantee. The 500-event `trackBatch` input limit is independent of HTTP batching: when all payloads and capacity fit, one 500-input operation plus a configured session prefix can commit 501 records atomically and send them in multiple requests. Oversized operations are rejected before eviction; otherwise oldest records can be evicted atomically to make room. Expiry is checked during SDK activity and is not physical deletion at exactly seven days while the page is closed. Browser eviction, user clearing, private mode, and storage failures can remove or block data. IndexedDB failure does not fall back to memory or localStorage durability.

On transient delivery failures, records remain durably queued. With the default `maxRetries: 2`, the sender makes an initial attempt and up to two retries, then waits for the 60-second `retryCooldownMs` (or longer when a readable `Retry-After` requires it) before starting another retry cycle. `backoffMs` controls the capped exponential jitter between attempts. Retries do not regenerate or refresh event IDs, timestamps, or frozen payload bytes; seven-day expiry remains anchored to the original enqueue time.

The current database format is version 1. An unsupported, higher, lower, or incompatible existing format fails closed; initialization does not clear it, fork a second database, or copy queued data elsewhere. Changing `appId` or `environment` selects a different scoped database and does not migrate the prior queue.

`track`/`trackBatch` return `durablyStored` only after the IndexedDB transaction completes. For a batch, admission is all-or-nothing. A successful result reports the input event IDs/count; an optional session prefix is separate. This local status is not a Collector acknowledgement or ClickHouse visibility.

## Delivery and recovery

`autoFlush` defaults to `true`. The SDK can also run a bounded attempt through `flush({ timeoutMs })`. Offline state, missing consent, lifecycle suspension, lock contention, a latched fault, and request limits can leave data pending. Delivery uses the existing Collector `POST /v1/events:batch` request with `X-API-Key`. It splits requests by event count and complete UTF-8 body size; it removes records only after a 2xx response body reports an integer `accepted_events` equal to the attempted count.

The public client methods are `track`, `trackBatch`, `confirmConsent`, `withdrawConsent`, `flush`, `getDiagnostics`, `listQuarantined`, `retryQuarantined`, `discardQuarantined`, `validateAndResume`, and `dispose`. `trackBatch` reports input event IDs/count; if a configured session prefix is inserted, it is returned separately as `prefixEventId` and is not included in the input count.

Delivery is at least once, not exactly once. If the Collector accepts an event but the acknowledgement is lost, the same event ID and frozen payload can be replayed. A timeout during the full request/body-read deadline is transient and schedules a retry; an incomplete body caused by that timeout is not treated as a malformed successful response. A completed 2xx response with an unreadable, oversized, or malformed body, or an incorrect `accepted_events` count, latches a fault and retains the records. A `400`, `422`, single-event `413`, authentication/route failure, or unexpected response also does not automatically delete rejected data. Faulted/quarantined records require explicit host action.

Use `getDiagnostics()` for local consent state, safe counts/revisions and fault state. Its `senderActive`, `lastBusy`, and `withdrawalIncomplete` flags describe this client instance; they are not cross-tab authority. Use `listQuarantined()` to retrieve current opaque handles. The handle list contains no event payload. For explicit review, use current diagnostics revisions when invoking a recovery method:

```js
const diagnostics = await client.getDiagnostics()
const quarantined = await client.listQuarantined()

if (quarantined.length > 0) {
  const result = await client.retryQuarantined(
    quarantined.map((record) => record.handle),
    {
      expectedFaultRevision: diagnostics.faultRevision,
      expectedQueueRevision: diagnostics.queueRevision,
      reasonCode: "configurationCorrected",
      policyVersion: "game-recovery-v1",
    },
  )
}
```

`retryQuarantined` and `discardQuarantined` require explicit handles, `expectedFaultRevision`, `expectedQueueRevision`, a supported camel-case `reasonCode`, and `policyVersion` (1–64 letters, digits, `_`, `.`, or `-`). Retry reasons are `configurationCorrected`, `collectorLimitCorrected`, or `sdkFixValidated`; discard reasons are `invalidFrozenEvent`, `collectorContractRejected`, or `unsupportedFrozenPayload`. They commit a local retry/discard decision; they do not prove Collector or ClickHouse acceptance. `validateAndResume()` also requires both current revisions, `policyVersion`, and one of `configurationCorrected`, `collectorLimitCorrected`, `sdkFixValidated`, or `postDisposalValidation`. A probe-required fault is validated using one real eligible pending event and the normal Collector acknowledgement rules; the SDK does not generate a synthetic probe. If no eligible event remains or quarantine is unresolved, recovery remains pending and the fault stays held. Never automatically discard events to clear a fault.

## Withdrawal and endpoint migration

`withdrawConsent()` disables this client locally immediately and acquires the scoped revocation Web Lock before writing its host-journal marker. It holds that lock across the IndexedDB purge and compare-clear, then releases it; this serializes withdrawal with grants, admissions, dispatch start, and endpoint migration. When both locks are needed, the order is sender-owner → revocation → state; withdrawal takes revocation → state and never waits for sender ownership. The sender holds revocation through synchronous fetch invocation, so a pending marker prevents cooperating SDK instances from starting later requests even when advisory broadcasts are lost. Migration takes sender-owner → revocation → state. Treat `globalWithdrawalCommitted: true` as confirmation that the shared purge committed; `revocationCleared: true` separately confirms marker removal. If queue storage/coordination fails, the marker remains for restart repair when it was persisted. If marker persistence or lock acquisition fails, retain the host's denied consent state; if clearing fails after purge, do not grant until the marker is repaired.

The queue purge barrier coordinates cooperating **browser-game SDK instances only**. It does not control other scripts, other origins, or server producers. A request already invoked, a CORS preflight continuation, or a host service worker may continue after withdrawal; the SDK cannot recall it or undo Collector acceptance. Do not run a separate direct-send client beside this client when relying on the shared withdrawal fence.

Changing `collectorUrl` for an existing app/environment store requires `previousCollectorUrl` to match the stored endpoint. The migration keeps that scoped installation/queue, updates the endpoint fence, and requires fresh permission. It does not copy records to a second store, rewrite event data, or recall requests already sent to the previous destination. Coordinate the change across open tabs and deploy the new Collector CORS policy first.

## Collector CORS and game CSP

For a cross-origin game, allow the exact game origin and the `POST`/`OPTIONS` methods. Allow `Content-Type` and `X-API-Key` request headers. In the Collector deployment configuration these are controlled by `CORS_ALLOW_ORIGINS`, `CORS_ALLOW_METHODS`, and `CORS_ALLOW_HEADERS`; do not use a wildcard origin for a credential-bearing application policy. Set the game's Content Security Policy `connect-src` to include the Collector origin.

The current Collector does not expose `Retry-After` by default to cross-origin JavaScript. The SDK uses its safe built-in backoff when that header is unreadable; no backend configuration or API change is required for this behavior.

## Browser security and validation

At initialization, the SDK feature-checks a secure context, top-level window, IndexedDB, Web Locks, Fetch, AbortController, `structuredClone`, `TextEncoder`, and secure random ID generation. It is not specified for workers, third-party iframes, or contexts missing those capabilities. Validate the required capabilities in the browsers and storage environments you support.

Run source, package, and optional browser checks from this directory:

```bash
npm test
npm run test:types
npm run build
npm run test:source-isolation -- --browser
```

The source-isolation command checks a clean source copy through tests, build, browser runs, packing, and consumer installation. Its results are local validation, not a guarantee for every browser or storage environment. Playwright WebKit is not Safari/iOS device validation, and browser lifecycle simulations do not certify real BFCache behavior.

## Local benchmark

Run the benchmark from this source directory:

```bash
npm run benchmark
```

Benchmark output is synthetic and environment-specific, not a performance guarantee. Near-cap operations currently load the store with `getAll()` into a snapshot rather than using an incremental cursor; large queues can therefore consume main-thread time. Measure frame impact with representative workloads on target devices.
