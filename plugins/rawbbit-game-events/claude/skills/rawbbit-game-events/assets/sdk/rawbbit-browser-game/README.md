# Rawbbit browser-game SDK

`@rawbbit/browser-game` is a consent-gated JavaScript client for browser games. It stores validated Collector events in IndexedDB before reporting them as accepted locally, then sends them to the existing Rawbbit Collector.

## Standalone source and local package

This directory is a self-contained source distribution. Copy the whole directory, including `vendor/rawbbit-client/`, which contains the local protocol modules used by the build. It does not import source from another Rawbbit SDK or require workspace tooling. Review changes to Collector request or event behavior against the Collector contract.

The source manifest describes the local ESM package `@rawbbit/browser-game@0.1.0`. To install dependencies, test, build, and create a local tarball:

```bash
# Run inside this source directory.
cd /path/to/rawbbit-browser-game
npm ci
npm test
npm run test:types
npm run build

# Optional: validate that a clean copy builds/packs without sibling source.
npm run test:source-isolation
# Add -- --browser to include the three-engine Playwright suite.
# npm run test:source-isolation -- --browser

destination="$(mktemp -d)"
npm pack --pack-destination "$destination"
GAME_DIR="/absolute/path/to/new-game"
npm install "$destination/rawbbit-browser-game-0.1.0.tgz" --prefix "$GAME_DIR"
```

`npm pack` builds a local tarball; it does not upload or publish it. See [INTEGRATION.md](./INTEGRATION.md) for host setup and release notes.

## Choose an integration path

- **Host-gated game integration:** the [consumer adapter template](./examples/GameAnalyticsAdapter.js.template) adds the shared synchronous host gate, permission-generation fence, durable-denial callback, and withdrawal repair protocol. Read the [host contract](./INTEGRATION.md#host-gated-adapter-contract) before using it. The game owns permission storage and game events, but every cooperating adapter must implement the same gate/generation contract.
- **Lower-level SDK integration:** `createBrowserGameClient()` provides SDK consent confirmation, queueing, and the revocation journal. It does **not** include the adapter's host gate or durable host-denial persistence. Use it only when your integration supplies and validates equivalent host coordination; a permission reader and revocation store alone are not the full host-gated protocol.

`createGameAnalyticsAdapter()` is copy-in example code, **not a maintained package export**. Copies do not automatically receive fixes: record which source version you copied, review differences when updating, and validate all cooperating game instances together. A future architectural improvement would be to package the generic consent facade as a maintained, versioned export while leaving permission storage and event vocabulary to each host.

## Lower-level SDK quick start

> This example demonstrates the direct client API, not a complete host-gated game integration. In particular, it does not close/persist a shared host denial gate when withdrawal fails before the SDK can write its revocation marker. For the additional host protocol, use the adapter contract linked above; do not bypass it with direct client calls.

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
  // Keep the withdrawal journal separate from the IndexedDB event queue.
  revocationStore: createLocalStorageRevocationStore({
    appId: "com.example.game",
    environment: "prod",
  }),
})

// Read the host game's current permission. Do not default this to true.
const permission = await client.confirmConsent(
  () => readCurrentAnalyticsStoragePermission() === true,
)

if (permission.status === "confirmed") {
  const result = await client.track({
    eventName: "round_completed",
    eventParams: { round_id: "round-17", score: 4200 },
  })

  if (result.status !== "durablyStored") {
    // Handle a safe status such as busy, invalidEvent, or storageFailure.
  }
}
```

Replace the example endpoint, key, app ID, environment, and permission reader with values supplied by the game. `collectorUrl` must be HTTPS except when `allowInsecureHttpForDevelopment: true` is explicitly enabled for local development. The API key is an extractable, app-scoped ingest key for an untrusted browser client, never a backend, database, or administrator credential.

The SDK defines no event catalogue or game-engine integration. The game owns event names, payload schemas, snapshots, and the committed transition that calls `track`.

## Local result and delivery meaning

`track` and `trackBatch` resolve an object with a `status`. `durablyStored` means the detached event envelope and session state committed to IndexedDB; it does **not** mean that HTTP delivery succeeded or that the event is visible in ClickHouse. Events retain their event IDs and payloads for retries. Delivery is at least once, so a response lost after Collector acceptance can result in a duplicate.

Important statuses include `consentUnknown`, `consentWithdrawn`, `busy`, `invalidEvent`, and `storageFailure`. A delivery fault can pause upload while new valid events continue to be stored within configured bounds; inspect `getDiagnostics()` for a fault. Diagnostics include the local consent state and `senderActive`, `lastBusy`, and `withdrawalIncomplete` flags. Explicit recovery methods are documented in [INTEGRATION.md](./INTEGRATION.md).

`revocationStore` is required and must persist pending withdrawal independently of the IndexedDB event queue. Its `readPending()` returns the opaque token or `null`; `markPending()` persists and returns a fresh token; `clearPending(token)` compare-clears only that token and returns `true` only after durable commit. A false result or rejection must leave the marker intact. The included `createLocalStorageRevocationStore()` stores only an opaque random token scoped to origin, app ID, and environment; it never stores events, identity, endpoint, or API key. A host may instead supply an authoritative persistent privacy-settings store.

Withdrawal acquires the scoped revocation Web Lock, writes the pending marker **before** purging IndexedDB, and holds the lock through purge and marker clear. While the marker is pending, cooperating SDK clients cannot admit new events, grant consent, or start fetches. The adapter template adds a synchronous host permission gate and a separate scoped Web Lock around adapter confirmation and event admission; it rechecks permission and a host-generation fence at the actual Fetch invocation. The SDK and every cooperating adapter must use the same native or injected Web Locks manager; `config.locks` takes precedence over `config.runtime.locks`. Adapter wrapper inputs and event timestamps are detached synchronously before waiting for the host lock. The configured clock is used during SDK initialization and startup retention pruning as well as later track calls. After host withdrawal starts, cooperating adapters block new operations that reach the in-lock permission check. A track/trackBatch call that passed only the initial synchronous check but is still waiting for the host lock is checked again and denied. Only a call that passes the in-lock check and enters `client.track` or `client.trackBatch` is drained by that lock before withdrawal proceeds; the completed queue purge removes its event(s) before withdrawal returns. Before queueing those operations on the lock, the adapter reserves against configured `maxPendingTrackOperations` and aggregate `maxPendingTrackBytes`, returning `busy` when either bound is reached. If the purge fails, the host gate still blocks new Fetch starts and repair must finish before opt-in. Tracking calls made while the shared gate is already closed return denied immediately and cannot become post-re-consent admissions. `confirmCurrentPermission()` joins an active withdrawal or retries a required repair; it does not automatically grant while the gate is closed. `flush({ timeoutMs })` starts its deadline before waiting for the host lock, forwards only the remaining budget to SDK flush, and cancels its queued wait when the deadline expires. This also applies when SDK revocation-lock acquisition times out before marker write. The host must implement `isAnalyticsPermissionAllowed()` as a synchronous, fail-closed check and make `persistAnalyticsPermissionDenied()` close the shared gate and advance the host permission generation synchronously before its first `await`. `readAnalyticsPermissionGeneration()` is a required synchronous callback returning that same shared, monotonically increasing, non-reused safe integer; increment it on every host permission revocation/transition to denied. The Fetch wrapper captures the generation before requesting its lock and compares it again under the lock, so a delayed old request cannot become authorized after a later explicit re-consent. Persist the current generation with the host's durable permission authority so cooperating tabs and restarts read a consistent value. Immediate readback cannot prove cross-tab wiring or restart durability. The template does not expose its raw SDK client, preventing callers from bypassing the adapter gate. Earlier copies of the template must migrate `adapter.client.track(...)` to `trackCommittedTransition(...)`, `adapter.client.trackBatch(...)` to `trackCommittedTransitions(...)`, and `adapter.client.confirmConsent(...)` to `confirmCurrentPermission(...)`; this is a source-template migration change, not a published-package break. A Fetch already started before denial cannot be recalled. LocalStorage can be user-cleared, unavailable, or evicted, and neither it nor the SDK guarantees physical-media durability. Hosts needing stronger persistence must provide their own journal.

`globalWithdrawalCommitted` reports the IndexedDB purge barrier. `revocationCleared` separately reports whether the journal marker was cleared. A failure before purge commits returns `globalWithdrawalCommitted: false`, `revocationCleared: false`, and `localDisabled: true`. A completed purge with a pending marker returns `status: "withdrawn"`, `globalWithdrawalCommitted: true`, `revocationCleared: false`, `localDisabled: true`, and `safeDiagnosticCode: "revocationClearPending"`; consent remains blocked until repair. Journal read/write/validation errors appear as safe diagnostic codes, and `getDiagnostics().revocationStorageCode` exposes the current journal error. These gates complement, but cannot recall, requests that had already started.

The client methods are `track`, `trackBatch`, `confirmConsent`, `withdrawConsent`, `flush`, `getDiagnostics`, `listQuarantined`, `retryQuarantined`, `discardQuarantined`, `validateAndResume`, and `dispose`. `autoFlush` defaults to `true`; set it to `false` to disable automatic send attempts and use explicit `flush()` after permission is confirmed.

The adapter constructor requires the host callbacks `readCurrentAnalyticsPermission()`, `isAnalyticsPermissionAllowed()`, `readAnalyticsPermissionGeneration()`, and `persistAnalyticsPermissionDenied()`. Track wrappers also capture the shared host permission generation before waiting for the host lock and compare it again after acquisition. A call that had not passed the in-lock check before withdrawal cannot become a new admission after re-consent; only calls already inside the lock may finish, and withdrawal drains and purges them.

## Runtime requirements and validation

Initialization requires a secure, top-level browsing context with IndexedDB, Web Locks, Fetch, AbortController, `structuredClone`, `TextEncoder`, secure random ID generation, and a host revocation journal. Missing required capabilities fail closed; event records never fall back to localStorage or memory. The optional SDK journal helper uses localStorage for the small revocation token only.

Run the package scripts from this directory for unit, type, build, and source-isolation checks. `npm run test:source-isolation -- --browser` additionally validates a clean source copy through the browser, package, and installed-consumer checks. Browser support depends on the required capabilities described above; Playwright WebKit is not Safari/iOS device validation.

Local validation and tarball creation do not certify a browser/version support matrix.

For host consent, storage scope, queue limits, lifecycle, CORS/CSP, recovery, and endpoint migration, see [INTEGRATION.md](./INTEGRATION.md).
