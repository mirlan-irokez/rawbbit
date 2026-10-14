# Browser-game JavaScript SDK

The Rawbbit browser-game SDK is an ESM client for top-level browser games. It adds a host-confirmed analytics-permission gate, IndexedDB-backed local queue, cross-tab coordination between cooperating instances, and explicit delivery-fault recovery.

## Distribution and source build

The source is included in `sdk/rawbbit-browser-game/` and builds without other SDK folders or repository-root tooling. Its `vendor/rawbbit-client/` directory contains the local protocol modules used by the build. Copy the entire SDK folder when using the source elsewhere; review changes to Collector request or event behavior against the Collector contract.

The separate npm package is `@rawbbit/browser-game@0.1.0` and currently has `private: true`. It has not been published to npm; do not use `npm install @rawbbit/browser-game` from a registry. Install, test, build, and pack the SDK from its standalone folder:

```bash
# Run inside sdk/rawbbit-browser-game, or its copied folder.
cd /path/to/rawbbit-browser-game
npm ci
npm test
npm run test:types
npm run build

# Optional clean-copy isolation/pack/consumer verification; add -- --browser
# to include the three-engine Playwright suite.
npm run test:source-isolation

destination="$(mktemp -d)"
npm pack --pack-destination "$destination"
GAME_DIR="/absolute/path/to/new-game"
npm install "$destination/rawbbit-browser-game-0.1.0.tgz" --prefix "$GAME_DIR"
```

`npm pack` creates a local tarball, not a registry release. The public repository's root Apache-2.0 license covers the SDK source; publishing the npm package is a separate decision. If the tarball is distributed independently, include the license text with it.

## Initialization and host-owned events

**Lower-level SDK path:** the direct `createBrowserGameClient()` example below is not a complete host-gated game integration. It provides SDK consent confirmation and a revocation journal, but does not include the adapter's shared synchronous host gate, permission-generation fence, or durable host-denial callback. In particular, a host permission reader and journal alone do not close the pre-marker lock-failure gap. Direct-client integrators must supply and validate equivalent host coordination; do not bypass a host-gated adapter with direct client calls.

The [consumer adapter template](../sdk/rawbbit-browser-game/examples/GameAnalyticsAdapter.js.template) contains that additional protocol. It is copy-in example code, **not a maintained package export**; pin its source revision and review updates because SDK upgrades do not update copied adapters. A maintained, versioned generic consent facade would reduce this copy-in maintenance burden in the future, but is not required for source availability and is not an available API.

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
  autoFlush: true,
})

const consent = await client.confirmConsent(
  () => readCurrentAnalyticsStoragePermission() === true,
)

if (consent.status === "confirmed") {
  await client.track({
    eventName: "round_completed",
    eventParams: { round_id: "round-17", score: 4200 },
  })
}
```

Replace the placeholder values and permission reader in a game. `appId`, an explicit `environment`, an app-scoped `apiKey`, and an HTTPS `collectorUrl` are required; `prod` and `production` are distinct values. Plain HTTP requires the explicit development-only `allowInsecureHttpForDevelopment` option. Browser API keys are extractable and must not be backend, database, or administrator credentials. The key is held in page runtime for requests, not stored with events or included in diagnostics, but is accessible to same-origin page code.

The host owns event vocabulary, schemas, player/game snapshots, and the committed state transition that invokes `track` or `trackBatch`. The SDK does not instrument gameplay, add engine/framework hooks, or define an event catalogue. Inputs are detached JSON snapshots; arbitrary objects, cycles, accessors, unsupported values, and oversized data are rejected.

## Consent, local storage, and delivery

Every new client starts with unknown consent. `revocationStore` is required and persists pending withdrawal independently of the event queue's IndexedDB:

- `readPending()` returns a persisted opaque token or `null`.
- `markPending()` must persist and return a fresh token before queue purge begins.
- `clearPending(token)` must compare-and-swap and return `true` only after durable clear; `false` or rejection must leave the marker intact.

`createLocalStorageRevocationStore({ appId, environment })` provides an origin-local helper. It uses same-origin localStorage for one opaque random marker scoped by origin/app/environment, and stores no event, identity, endpoint, or API-key data. This is not an event-queue fallback. A host can provide an independent privacy-settings store with the same interface; its operations must be bounded and persist across SDK/client restarts. Neither localStorage nor the SDK guarantees physical-media durability; browser storage can be unavailable, cleared, or evicted.

`confirmConsent(permissionProvider)` checks this journal before invoking the host callback and again under the shared revocation lock before granting:

- A fenced boolean `true` grants the current client instance.
- A fenced boolean `false` commits withdrawal and purges the cooperating shared queue.
- A thrown, rejected, or non-boolean result leaves the client unknown and does not apply a global denial.
- Stale checks cannot grant or purge against a newer permission/store/endpoint state.
- The scoped revocation Web Lock serializes journal access, grant, event admission, fetch-start validation, and endpoint migration; lock order is sender-owner → revocation → state. Withdrawal holds revocation through marker write, purge and compare-clear; migration uses sender-owner → revocation → state.
- A pending marker returns `consentUnknown` / `withdrawalIncomplete` without calling the permission provider, granting, admitting new events, or starting fetches. Retry `withdrawConsent()` to purge the queue and clear the marker before requesting fresh permission.

There is no SDK consent screen. Absence of a prompt or UI is not permission. BFCache/lifecycle restoration invalidates the local permission and invokes optional `onPermissionRequired`; the host must re-check its current policy and confirm again before tracking or replay.

If queue purge cannot commit, the independent journal marker remains, so reinitialization after page/client restart blocks confirmation and sending while that marker remains available. `globalWithdrawalCommitted` reports the queue purge; `revocationCleared` separately reports journal cleanup. A failure before purge commit returns `globalWithdrawalCommitted: false`, `revocationCleared: false`, and `localDisabled: true`. If purge committed but marker cleanup did not, the result remains locally disabled and consent stays blocked until a later withdrawal retry clears the marker. Do not use confirmation, client recreation, or manual marker deletion to work around incomplete withdrawal.

If the journal itself cannot write/read/clear, keep the host's authoritative privacy state denied and do not reinitialize/regrant as a workaround. A missing/unavailable persistent store cannot provide a restart guarantee; the SDK reports a safe journal failure and keeps the current client disabled. Completed purge with failed marker cleanup reports `status: "withdrawn"`, `globalWithdrawalCommitted: true`, `revocationCleared: false`, `localDisabled: true`, and `safeDiagnosticCode: "revocationClearPending"`; journal diagnostics are available in `getDiagnostics().revocationStorageCode`. This journal is not an encrypted boundary against same-origin code and does not recall an HTTP request already started.

The consumer adapter template requires `readCurrentAnalyticsPermission()`, `isAnalyticsPermissionAllowed()`, `readAnalyticsPermissionGeneration()`, and `persistAnalyticsPermissionDenied()`. The permission check must be synchronous and return literal `true` only while consent is allowed. The generation reader must synchronously return a shared, monotonically increasing, non-reused **non-negative safe integer**; advance it synchronously whenever host permission is revoked or the shared gate closes. The denial callback must close that shared gate and advance the generation before its first `await`, then durably persist denial and the new generation to the same host authority used by the readers. The template serializes confirmation and event admission with a scoped host Web Lock and rechecks permission and generation at the actual Fetch-start boundary. It captures the generation before requesting the host lock and compares again after acquisition, preventing a delayed request from an older allowed period from sending after withdrawal and later re-consent. Adapter tracking also reserves the configured pending-operation count and aggregate snapshot-byte budget before waiting for that lock; exhausted capacity returns `busy`. Track wrapper inputs and timestamps are detached at call time. The configured SDK clock also applies during initialization and startup retention pruning. All cooperating adapters and the SDK must use the same native or injected Web Locks manager (`config.locks` takes precedence over `config.runtime.locks`). Permission callbacks must be bounded and must not re-enter the adapter. The template verifies an immediate false permission readback, but that check cannot prove cross-tab wiring or restart durability. This gates previously granted cooperating adapters if SDK revocation-lock acquisition times out before marker write. Tracking calls made while the gate is already closed return denied immediately and cannot turn into post-opt-in admissions. A call that passed only the initial check is rechecked under the host lock; only calls that pass the in-lock check and enter `client.track` or `client.trackBatch` are drained before withdrawal's queue purge. `confirmCurrentPermission()` joins an active withdrawal, retries a latched repair, or returns denied while the gate remains closed; it does not automatically grant. `flush({ timeoutMs })` starts its deadline before waiting for the host lock, cancels the queued request when that deadline expires, and forwards only the remaining budget to SDK flush. Keep the shared gate and host denied after any persistence failure or incomplete withdrawal; re-consent is permitted only after queue purge and marker clear succeed, followed by a separate explicit host opt-in. The template does not expose its raw SDK client; older copied templates must add `readAnalyticsPermissionGeneration()` and synchronously advance the shared generation on denial, then migrate `adapter.client.track(...)` to `trackCommittedTransition(...)`, `adapter.client.trackBatch(...)` to `trackCommittedTransitions(...)`, and `adapter.client.confirmConsent(...)` to `confirmCurrentPermission()`. This source-template migration does not change the private, unpublished package API. A Fetch already started before denial cannot be recalled.

The database is scoped by browser origin/storage partition, app ID, and environment, and shared across cooperating browser-game tabs. It creates a new namespace-scoped installation ID; it never automatically reads the legacy `rb_user_pseudo_id`, legacy sessions, or an old client's queue. The SDK uses its installation ID as the default `user.user_pseudo_id`, but the caller may supply an explicitly approved per-event pseudo-ID through `user.user_pseudo_id`/`user.userPseudoId`. There is no automatic identity linking across events, apps, origins, or storage partitions. A host may supply an explicitly reviewed `legacyIdentityImport` for a newly created empty store, with a pseudo-ID no longer than 256 characters, matching app/environment, and a `policyVersion` of 1–64 ASCII letters, digits, `_`, `.`, or `-`; this imports only the chosen pseudo-ID, not old sessions or events:

```js
legacyIdentityImport: {
  pseudoId: "reviewed-installation-id",
  appId: "com.example.game",
  environment: "prod",
  policyVersion: "identity-import-v1",
}
```

The option does not read or clear `localStorage`. An existing scoped identity is never replaced: a mismatching import is rejected, while repeated configuration with the stored pseudo-ID is allowed.

Cooperating tabs share a logical session. It expires after more than 30 minutes without accepted tracking activity; exactly 30 minutes retains the session. Upload, retry, reload, and visibility do not refresh it. If configured, the pure `sessionStartFactory` can add a consumer-defined prefix. The prefix and first triggering event are committed atomically with the same captured event timestamp; the prefix uses session sequence 1 and the trigger sequence 2. No event is generated unless the consumer configures it. `eventContextMapper` can map consumer-owned properties in supported event context maps but cannot map `user`/identity or override SDK-owned fields and `device.platform`. Both callbacks must be synchronous, pure, non-reentrant, and side-effect free; the SDK can recompute them on a session revision conflict for at most three candidate/admission attempts before returning `busy`.

The runtime defaults are explicit configuration values:

| Config field | Default | Purpose |
| --- | ---: | --- |
| `maxQueueBytes` | 10,485,760 bytes (10 MiB) | Logical pending + quarantine limit |
| `retentionDays` | 7 | Eligibility from original enqueue time |
| `sessionTimeoutMs` | 1,800,000 ms (30 minutes) | Shared session inactivity threshold |
| `maxEventsPerRequest` | 500 | Event count per Collector request |
| `maxRequestBytes` | 1,048,576 bytes (1 MiB) | Serialized `{events:[...]}` request body cap |
| `maxTrackBatchEvents` | 500 | Input event count per `trackBatch` |
| `maxPendingTrackOperations` | 64 | Detached track handoff operations |
| `maxPendingTrackBytes` | 2,097,152 bytes (2 MiB) | Aggregate detached track handoff byte budget |
| `timeoutMs` | 10,000 ms | Full request/body-read deadline |
| `maxRetries` | 2 | Retries after the initial send in each cycle |
| `backoffMs` | 250 ms | Base for capped exponential jittered backoff |
| `retryCooldownMs` | 60,000 ms | Cooldown after initial send plus two retries |
| `flushTimeoutMs` | 15,000 ms | Default `flush()` deadline |
| `lockTimeoutMs` | 3,000 ms | State-lock wait deadline; never steals a held lock |
| `storageTimeoutMs` | 3,000 ms | IndexedDB open/transaction deadline |
| `maxResponseBytes` | 65,536 bytes (64 KiB) | Response body byte cap |
| `maxSnapshotDepth` | 32 | JSON nesting depth |
| `autoFlush` | `true` | Automatic delivery attempts |

Each queued event uses its frozen UTF-8 payload size plus 512 logical metadata bytes for queue accounting; this is not a browser quota or physical-storage guarantee. A 500-input `trackBatch` plus a configured prefix can atomically enqueue 501 records when all snapshots fit; upload then splits by request count and byte limits. Oversized operations are rejected before eviction; otherwise oldest records may be evicted. Expiry is checked during SDK activity, not while the page is closed. Storage failure has no memory/localStorage durability fallback. On transient delivery failures, records remain durably queued. With the default `maxRetries: 2`, the sender makes an initial attempt and up to two retries, then waits for the 60-second `retryCooldownMs` (or longer when a readable `Retry-After` requires it) before starting another cycle; `backoffMs` controls the capped exponential jitter between attempts. Retries preserve frozen event IDs, timestamps, and payload bytes; retention remains measured from original enqueue time.

The current local database format is version 1. An unsupported, higher, lower, or incompatible existing format fails closed without clearing it, creating a second namespace, or copying records elsewhere. Changing `appId` or `environment` selects a separate scoped database; it does not migrate the previous queue.

`track`/`trackBatch` return `durablyStored` only after IndexedDB transaction completion. This means local acceptance, not an HTTP response or ClickHouse visibility. HTTP sends are at least once; an ambiguous/lost acknowledgement can produce a duplicate with the original event ID and payload. Records are removed only after a 2xx response confirms the exact `accepted_events` count. A timeout during the full request/body-read deadline is transient and schedules a retry; it is not treated as a malformed successful body. A completed 2xx response with an unreadable, oversized, or malformed body, or an incorrect `accepted_events` count, latches a fault and retains the records.

`autoFlush` defaults to `true`; `flush()` requests a bounded explicit send pass. Offline, consent, lifecycle, storage, coordination, retry, or delivery faults can leave records pending. `getDiagnostics()` reports local consent state, safe counts/revisions, and fault metadata without keys or event bodies. It also reports client-local `senderActive`, `lastBusy`, and `withdrawalIncomplete` flags; these are not cross-tab authority. Quarantine handles from `listQuarantined()` are opaque and must be explicitly reviewed. `retryQuarantined`, `discardQuarantined`, and `validateAndResume` require expected queue/fault revisions and reason/policy values. Retry/discard is a local action, not a Collector or ClickHouse acknowledgement. Probe recovery uses one real eligible queued event; the SDK does not fabricate a probe or automatically delete rejected records.

The client methods are `track`, `trackBatch`, `confirmConsent`, `withdrawConsent`, `flush`, `getDiagnostics`, `listQuarantined`, `retryQuarantined`, `discardQuarantined`, `validateAndResume`, and `dispose`. Quarantine retry reason codes are `configurationCorrected`, `collectorLimitCorrected`, and `sdkFixValidated`; discard codes are `invalidFrozenEvent`, `collectorContractRejected`, and `unsupportedFrozenPayload`. Resume also accepts `postDisposalValidation`. All recovery calls require the current `expectedFaultRevision`, `expectedQueueRevision`, and a 1–64 character alphanumeric/`_`/`.`/`-` `policyVersion`.

`readCurrentAnalyticsPermission()` and `isAnalyticsPermissionAllowed()` must consult the same host gate. Track wrappers capture the shared generation before waiting for the host lock and compare it again after acquisition. A delayed call that did not pass the in-lock check before withdrawal is denied even if re-consent occurs before its lock request runs; calls already inside the lock may finish and are drained before purge. Older copied adapter templates must add the generation reader and synchronously advance the generation when denial begins, in addition to migrating `adapter.client.track`, `trackBatch`, and `confirmConsent` calls to the current wrappers.

## Withdrawal and endpoint changes

`withdrawConsent()` immediately disables the caller, purges pending/quarantined records, and resets the session while retaining installation identity. The journal ordering, failure behavior, and result fields are described under Consent, local storage, and delivery above.

The queue purge barrier coordinates only cooperating instances of this new SDK. It does not control old direct-send clients, other scripts, other origins, or server producers. A request already invoked, a continuing CORS preflight, or a host service worker may proceed after withdrawal. The SDK cannot recall requests or reverse Collector acceptance.

The queue is bound to the Collector endpoint. A changed URL requires `previousCollectorUrl` matching the stored URL. Explicit migration retains the same scoped identity and queue, updates the endpoint fence, and requires fresh host permission; it does not copy events to another store or recall requests already sent to the old endpoint. Coordinate migration across open game tabs.

## CORS, CSP, and browser requirements

For a cross-origin deployment, configure the Collector to allow the exact game origin, `POST` and `OPTIONS`, and request headers `Content-Type` and `X-API-Key`. The Collector's `CORS_ALLOW_ORIGINS`, `CORS_ALLOW_METHODS`, and `CORS_ALLOW_HEADERS` settings control these values. Set the game's CSP `connect-src` to permit the Collector origin. The current Collector does not expose `Retry-After` by default to cross-origin JavaScript; the SDK uses safe built-in backoff when unreadable.

The client requires a secure top-level context with IndexedDB, Web Locks, Fetch, AbortController, `structuredClone`, `TextEncoder`, and secure random IDs. Workers, third-party iframes, and contexts without these capabilities are not supported by this contract. This feature list is not a browser/version certification; no exact support matrix is established.

Local verification passed 159 browser-game Node tests, type checks, build/prepack, and installed ESM and NodeNext consumer checks from an isolated source copy. The current local tarball contains seven files. A separate earlier run passed 138 Playwright tests (46 each for Chromium, Firefox, and WebKit); those browser tests were not rerun after the nested license file was removed. The license removal did not change runtime code.

The cached local run does not establish cold CI timing: Ubuntu Node 22/24 matrix runs and completion of a cold CI run within 25 minutes remain unverified. Actual Safari/mobile devices, real BFCache/native-freeze behavior, and private-browsing/storage eviction also remain unverified. Playwright WebKit is not Safari/iOS device validation, and no browser/version support matrix is certified. The npm package remains private and unpublished; the source is available in this repository.

One local headless Chromium benchmark run on darwin25.5.0 arm64 reported 10,438,272 logical queue bytes across 576 seeded records, a 900,000-byte snapshot, initialization 19.2 ms, seeding 322.3 ms, large/near-cap admission 65.9 ms, diagnostics 48 ms, purge 56.3 ms, and a maximum observed animation-frame gap of 16.8 ms. Run it from the repository root with `(cd sdk/rawbbit-browser-game && npm run benchmark)`. This single synthetic result is not a performance guarantee or evidence of zero gameplay cost. Near-cap operations currently use a `getAll()` store snapshot rather than an incremental cursor, so large queues can consume main-thread time. Mobile hardware and a real game workload have not been measured.

## Package publication and production rollout

The source is available in this repository; that does not publish the npm package or establish production browser support. Before a separate package release or game rollout, address the applicable decisions:

- For npm publication: decide the registry name, visibility, version, and release approval; remove `private: true` only as part of that decision.
- For a separately distributed tarball: include the Apache-2.0 license text and review the packed artifact and dependencies.
- For each game: validate the host-owned consent gate, copied-adapter revision/update ownership, and withdrawal/restart behavior. A maintained generic facade remains a future improvement, not a prerequisite for publishing source.
- For production rollout: review origin/app/environment scope and privacy, including the extractable browser key and storage/withdrawal limits; establish a browser/OS/device test matrix that distinguishes Node/fake IndexedDB, Playwright engines, and actual Safari/mobile hardware; plan migration from any legacy sender.

The npm package remains private and unpublished, and no production browser-support matrix is certified.
