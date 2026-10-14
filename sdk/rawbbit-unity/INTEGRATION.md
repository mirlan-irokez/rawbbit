# Integrate Rawbbit into a Unity game

This is the reusable copy-in workflow for a new game and for AI coding agents assisting with instrumentation. Rawbbit Unity runtime answers **how** a JSON event is persisted and sent. The consumer game's adapter answers **what happened**, what game-state snapshot was true then, and which committed transition owns the event.

## 1. Inspect before changing a game

Before proposing or writing instrumentation, inspect the consumer project and its own approved analytics specification:

1. Read `ProjectSettings/ProjectVersion.txt`; identify actual Unity and scripting backend versions. Do not infer compatibility from this SDK's provisional 2022.3 LTS baseline.
2. Identify Android/iOS build targets, scene/bootstrap lifecycle, existing analytics/network/storage packages, app configuration/deep-link/remote-config policy, save/resume model, locale source and consent owner.
3. Locate canonical game state and successful state-commit boundaries for candidate events. Do not infer event triggers from UI animation, pointer callbacks, UI labels, or this template.
4. Read the game's event contract and write down event grain, required fields, null/empty rules, snapshots, IDs/counters, ordering, resume behavior, consent and schema version. Ask before filling ambiguities.
5. Identify the approved app ID, explicit environment names, Collector URL, per-app ingest key source, data retention/deletion policy and who can authorize recovery disposal. Never inspect or print secret values for documentation.
6. Report proposed files and event trigger locations before editing if the task has not explicitly authorized code changes.

**Agent guardrail:** do not copy event names, properties, inventories or gameplay triggers from a sample or another game unless the target game's contract explicitly adopts them. The runtime must remain content-agnostic. Game-specific instrumentation belongs in the consumer project and requires separate authorization.

## 2. Copy the SDK and template

Copy the complete `sdk/rawbbit-unity/Runtime/` directory into the game as `Assets/Rawbbit/`. Preserve its `.asmdef`. Install `com.unity.nuget.newtonsoft-json` using the Package Manager version compatible with the game's Unity editor; its precompiled `Newtonsoft.Json.dll` is expected to be included through Unity's default precompiled references (`overrideReferences` is disabled), but compile-verify this on the target editor. Do not vendor a duplicate Newtonsoft DLL. The exact package version remains a release gate because no target Unity Editor was available to verify the package graph here.

Copy `Examples/GameAnalyticsTemplate/GameAnalyticsAdapter.cs.template` into the game-owned analytics directory and rename the extension to `.cs`. Replace `YourGame.Analytics`, configuration accessors and marked event mapping. The `.template` file is intentionally not compiled by Unity.

Recommended ownership:

```text
Assets/Rawbbit/                    # generic Rawbbit SDK; do not edit per game
Assets/Game/Analytics/              # game's consent/config/event adapter
Assets/Game/Gameplay/               # authoritative transition/state changes
```

Do not call SDK code from every gameplay component. Route through the adapter.

## 3. Configure one persistent client

Call `RawbbitUnityHost.Create(...)` on Unity's main thread, once from a durable bootstrap, not on every scene: it synchronously reads Unity platform/version/system information before starting store initialization. It creates one `DontDestroyOnLoad` host and owns a single sender. Configure:

- Current Collector HTTPS base URL or full `.../v1/events:batch` endpoint. `PreviousCollectorUrl` is optional; it identifies the old Collector URL for an explicitly authorized endpoint change and does not change the request destination. Shared configuration may include it for fresh installs when the fresh-store guard permits creation.
- Per-game app-scoped ingest key from the approved runtime configuration source. Mobile client keys are extractable and must not be treated as secrets.
- One `app_id` shared by Android and iOS. The SDK chooses `device.platform` (`android` or `ios`); Editor traffic reports `unity_editor`, and other targets report `unity_other`.
- The Unity host captures `app_version`, `os_version`, and `device_model` defaults on the main thread. Configured `DefaultDevice` values suppress automatic defaults for matching keys; per-event `Device` values then override configured/default values, including explicit `null`, except `platform`, which remains SDK-owned. Locale is not inferred from Unity culture; hardware identifiers are not collected.
- Explicit environment. Use the exact value analytics queries use; Collector does not force `prod` vs `production` consistency.
- Optional product, surface, source type and schema version defaults. Event-level values may override defaults.

Wait until `host.IsReady` before use and fail closed if `InitializationFailed`. Use `Client` or host forwarding methods only after readiness. `RawbbitUnityHost.Create` is intended once per player; a second active host throws to prevent duplicate senders. Collector requests set `redirectLimit = 0`; verify redirect handling on the target Unity version. Call the host's recovery-resume method from the Unity main thread; it performs probe transport through the host's main-thread `UnityWebRequest` pump.

The SDK starts consent as unknown at each process initialization. The host must ask its policy/consent owner for the current decision. 
Call `ConfirmConsentAsync()` only when collection is currently permitted; this releases eligible persisted events. 
Do not persist the SDK's previous in-memory grant as proof. Unknown itself neither authorizes tracking/dispatch nor invokes withdrawal, but ordinary expiry maintenance can remove expired records and startup completes any previously committed purge before a later grant. 
If consent is explicitly withdrawn/denied, call `WithdrawConsentAsync()` instead of merely omitting confirmation: it blocks new sends, durably purges local records and resets session state while retaining the installation ID. 
It does not retract an already-started request or Collector/server data.

The SDK supplies no consent UI. Integrate with the game's existing permission mechanism; do not automatically confirm permission merely because no consent screen exists. See [Consent responsibility](README.md#consent-responsibility) in the README.

### Persistent-store upgrades and endpoint changes

The queue now uses `Application.persistentDataPath/Rawbbit/store-v2-<hash(AppId, Environment)>`; the identity excludes the endpoint. The persistent format remains version 1 with additive `CollectorEndpoint` and transaction fields. An upgrade that keeps the same Collector URL automatically adopts its old URL-keyed store. To change endpoints on the first upgraded launch, identify the old URL explicitly:

```csharp
var config = new RawbbitConfiguration
{
    CollectorUrl = "https://collector-new.example/v1/events:batch",
    PreviousCollectorUrl = "https://collector-old.example/v1/events:batch",
    ApiKey = runtime.AppScopedApiKey,
    AppId = "your.game",
    Environment = runtime.Environment
};
var host = RawbbitUnityHost.Create(config);
```

Authorize an endpoint change only for the same logical Collector app/environment and an app-scoped key approved for the destination. A same-URL upgrade automatically adopts the matching old URL-keyed store; a changed URL on first upgrade requires `PreviousCollectorUrl` to identify the old store. The fresh-store guard applies only when no stable, current-endpoint legacy, or `PreviousCollectorUrl` legacy store is selected. Existing selection, adoption, conflict, selected-store corruption, and endpoint-binding behavior is unchanged. By default, a fresh install may initialize with shared `PreviousCollectorUrl` configuration when the `Rawbbit` root passes the fresh-store checks. An unrelated stable sibling is recognized only if its directory basename is exactly `store-v2-` followed by 32 lowercase hexadecimal characters (`[0-9a-f]`); a loose `store-v2-` prefix is not sufficient.

A v1 legacy directory hash includes the Collector endpoint, app ID, and environment. Without the old endpoint, the SDK cannot reliably establish that the selected app/environment has no v1 store at another URL. An unresolved legacy sibling therefore blocks fresh initialization by default and remains untouched. On the fresh-store path, root-level files and directory names not recognized by the strict stable/legacy name rules always block initialization, whether or not the opt-in is enabled. The guard classifies sibling entry names; it does not scan or deserialize sibling contents. A selected nonempty stable/legacy directory without a recognized snapshot or journal still fails closed and remains untouched rather than being initialized empty. A stable store alongside a known legacy root, or multiple known legacy roots, fails closed without merging or deleting stores.

`AllowFreshStoreWithUnresolvedLegacyStores` is a default-`false` host assertion for a verified new app ID/environment that has no old v1 store at any unspecified endpoint. On the fresh-store path, it permits proceeding despite unresolved legacy sibling directories only when each such directory basename is exactly 32 lowercase hexadecimal characters (`[0-9a-f]`). Root-level files always block fresh creation. Directory siblings must either use the strict unrelated-stable name above or, with the assertion enabled, the exact legacy-sibling name; every other directory basename blocks even with the assertion enabled. These directory names are not proof that a sibling contains v1 data or belongs to an unrelated app; the host's verified-new assertion is still required. Siblings remain untouched, and the SDK does not scan or deserialize their contents. Misuse can silently strand the app/environment's old queue and installation identity. This is not an endpoint-migration shortcut: use `PreviousCollectorUrl` for a known existing app/environment move. The assertion does not bypass selected-store checks or alter existing stable/current/previous legacy selection and adoption.

Enable the assertion only for the first creation of a verified new namespace, then remove it or set it back to `false`:

```csharp
var config = new RawbbitConfiguration
{
    CollectorUrl = runtime.CollectorUrl,
    ApiKey = runtime.AppScopedApiKey,
    AppId = "new.game",
    Environment = "production",
    AllowFreshStoreWithUnresolvedLegacyStores = true
};
var host = RawbbitUnityHost.Create(config);
```

This setting changes local store initialization only; it does not change the event schema, Collector API, or authentication behavior.

If an otherwise-unrecognized selected directory contains only SDK-owned sequence-zero checkpoint temps (`snapshot-00000000000000000000.json.<32-digit GUID>.tmp`), the SDK removes those uncommitted initial-write files and retries initialization. They precede every identity/event commit. Mixed unknown contents, invalid GUID names, and later-checkpoint temps still fail closed and remain untouched.

On an unambiguous migration, installation/session identity, queued and quarantined payloads, retention timestamps, and the delivery-fault latch are retained; normal expiry processing still applies. Endpoint binding is committed in the legacy directory before rename, so a crash on either side of the rename preserves the binding and queue. An actual endpoint change resets probe validation and revises the token only for retained probe-required faults, requiring a fresh probe against the new destination. Non-probe faults retain their revision and do not acquire a probe requirement. Migration and fresh consent do not clear a delivery fault; confirm current consent and follow the explicit recovery flow when required.

Downgrading to an SDK build that uses the old URL-keyed store layout is unsupported: it opens a different directory from the new stable store, so migrated/new data may be stranded from it. Do not copy or merge store directories to roll back.

## 4. Build the game-owned adapter

The adapter should own:

- Game-specific event names and schema mappings.
- The game's consent decision and explicit withdraw/reset wiring.
- Event-time business parameters and deep/immutable `user_properties` snapshots.
- Gameplay/session-attempt/tutorial IDs and sequences that belong to game saves.
- A pure session-start factory if the game schema has a `session_started` event. It runs synchronously on a worker thread, must not call Unity APIs, and receives one SDK session ID, the trigger-time event snapshot, `SessionStartedEventSequence=1`, and `TriggeringEventSequence=2`. The SDK queues its returned event before the triggering event. With no factory, the first tracked event has generic sequence 1. The SDK does not insert game sequence fields automatically.
- An optional `RawbbitEventContextMapper` that runs for every trigger if the game's schema needs the SDK-provided generic session sequence. It receives the frozen event input and sequence for the triggering event; the adapter chooses its own field name or can omit the mapper. It must be pure and must not call Unity APIs.
- Stable game account ID only if approved. Otherwise let the SDK use its persistent app-generated installation pseudo-ID; do not use hardware/advertising identifiers. Android/iOS share `app_id`, but their local installation identities are not automatically linked.
- An optional `IRawbbitSessionIdProvider` may replace default UUID generation. It must return a fresh, non-empty, control-character-free ID for each new SDK session; it must not restore a pre-withdrawal session ID from a game cache.

An adapter tracking method should accept a fully captured `RawbbitEventInput` and call `TrackAsync` after a game transition is committed. Provide only JSON-compatible values (null, strings, booleans, finite numbers, arrays, string-keyed dictionaries). Collector context fields are schema-typed (user IDs/device/traffic-source/geo values are strings or null; device keys are the supported Collector fields), while `event_params` and `user_properties` accept arbitrary nested JSON. Do not pass `GameObject`, `MonoBehaviour`, Unity assets, mutable live state, non-finite numbers, or raw free text/receipts. The SDK snapshots input before returning; the adapter remains responsible for capturing the correct historical state before invoking it.

Use the returned `RawbbitTrackResult` to distinguish `DurablyStored` from unknown consent, invalid payload, busy pre-persistence capacity and storage failure. It reports a safe diagnostic code, never a reason containing payload data. Do not block gameplay on upload or claim that local persistence equals delivery.

## 5. Session behavior

The SDK default is 30 minutes of **tracking inactivity**, configurable per game. Exactly 30 minutes stays in the same session; more than 30 minutes creates a new session on the next permitted tracked event. Track calls, not upload attempts, update the clock; offline delay does not rewrite queued session IDs. SDK assigns a monotonically increasing per-session generic sequence (prefix 1/trigger 2 when the adapter emits session-start; otherwise first event 1), but the adapter decides whether/how that value appears in its schema.

On a new session the adapter may create a game event using only supplied frozen event-time context. Do not query mutable game state from that factory. A session boundary does not restart or abandon a chapter/run/tutorial. Explicit consent withdrawal is different: after its durable purge barrier, the SDK clears session ID, activity time and session counter while retaining installation identity. Re-consent starts a fresh session on the next permitted event even if the 30-minute timeout did not elapse. Unknown startup alone does not reset persisted session state.

## 6. Queue, delivery and privacy limits

- Durable local FIFO queue: 10 MiB logical record budget and seven-day enqueue-age retention. Oldest records can be evicted to admit a new record; eviction/expiry/disposal means analytics loss. Local store uses app persistent data path and is **not encrypted by this SDK**. Check Android/iOS backup behavior and the game's privacy requirements.
- The session-start + trigger pair is initially committed atomically. A crash before queue commit can lose it; SDK won't persist only one half as if the transition succeeded.
- Requests are capped at 500 events and 1 MiB encoded JSON body, or lower configured limits. The body limit includes JSON wrapper/escaping/UTF-8. The Collector settings may be lower at deployment; configure no higher than its effective limits.
- Requests retry network/timeout/408/429/5xx with bounded attempts and cooldown. At-least-once delivery allows duplicates after ambiguous responses; IDs and frozen payloads remain stable.
- Immediately before transport, the worker-state and queue-store gates use nonblocking `Monitor.TryEnter`; contention on those gates defers dispatch instead of waiting for disk work or adapter callbacks. The short consent gate remains serialized. After acquiring the store lock, the SDK reads the clock and revalidates each selected record's presence and unchanged identity/payload, pending status, and expiry, as well as store health, withdrawal/purge barriers and fault state. Recovery probes must still match the current fault revision and queue generation. This boundary checks expiry in memory without pruning disk. The ordinary sender drops stale or deferred batch snapshots and prepares again on a later pump without recording a network failure or retry penalty; a declined recovery probe requires another explicit recovery call. Ordinary dispatch still requires current consent.
- A fault whose persistent `RecoveryProbeRequired` flag is set must pass explicit, current-consent recovery probing before clearance (this includes server faults and local frozen-event validation faults): the host sends one already-queued valid event, requires the exact `accepted_events: 1` response, and removes only that event. HTTP 400/413/422 during a probe re-quarantines that event; network errors, 408/429/5xx, or accepted-count mismatch leave it pending while the latch remains. No eligible queued event means recovery is pending; the SDK never manufactures probe analytics or drains the queue.
- HTTP 400/422 quarantines every event in the rejected batch; a single-event 413 is quarantined. Sender stops rather than silently deleting a potentially whole-SDK defect. Batch 413 reduces event count. Quarantine counts against cap and retention.
- Withdrawal stops new dispatch and purges remaining local queue, but a request already started may be accepted. The Collector has no API to retract accepted events.
- App termination, uninstall, OS suspension, disk errors, full storage, expiry and capacity eviction can prevent delivery. This SDK does not claim exactly once or guaranteed delivery.

## 7. Explicit delivery-fault recovery

Use this only after diagnosing/correcting the deployed configuration or SDK defect. Retry, disposal, validation, and probe mutations all require current consent; read-only diagnostics and quarantine listing do not. HTTP status alone never authorizes deletion. Recovery IDs are opaque local handles, not event IDs.

1. Surface `GetDiagnostics()` and `ListQuarantinedRecords()` through a safe operator/debug UI; omit event payloads, user IDs, session IDs, API key and response body.
2. Refresh diagnostics and retain both `FaultRevision` and `QueueGeneration` for optimistic concurrency.
3. If original frozen records remain valid (for example, only the deployed batch limit changed), call `RetryQuarantinedAsync(handles, faultRevision, queueGeneration, reasonCode, policyVersion)` under fresh consent. Allowed reason codes are `configuration_corrected`, `collector_limit_corrected`, or `sdk_fix_validated`. This unquarantines only the selected original payloads; it does not repair them. Refresh diagnostics afterward.
4. If selected frozen bytes are invalid and cannot be migrated, under current consent explicitly obtain authorization for data loss and call `DiscardQuarantinedAsync(...)` for exact reviewed handles. Allowed reason codes are `invalid_frozen_event`, `collector_contract_rejected`, and `unsupported_frozen_payload`. This commits a bounded payload-free receipt and does not clear the sender-fault latch. Do not automatically delete on 400/422/413 or on a later SDK upgrade.
5. Under current consent, re-read diagnostics; validate remaining local envelopes after the correction, ensure no quarantine/storage/purge fault remains, and call `RawbbitUnityHost.ValidateAndResumeAsync(faultRevision, queueGeneration, reasonCode, policyVersion)` from the Unity main thread. Allowed validation reasons are `configuration_corrected`, `collector_limit_corrected`, `sdk_fix_validated`, and `post_disposal_validation`. When the fault's persistent `RecoveryProbeRequired` flag is set—including for local frozen-event validation faults—the host performs one consent-gated probe using a real pending event and requires the exact accepted count; no pending event returns a probe-pending failure. Probe HTTP 400/413/422 re-quarantines its event; network errors, 408/429/5xx and acceptance-count mismatches leave it pending. Other non-retryable responses (for example 401/403/404 or a redirect response) leave the event pending and keep the fault latched. Failure keeps sends paused. A fresh consent grant does not clear a fault. Any payload migration requires a separately reviewed versioned policy; v1 only supports targeted disposal or retry of unchanged bytes.
   - If validation finds invalid pending (not previously quarantined) frozen records, it durably quarantines them and returns `invalid_records_quarantined_for_review` rather than deleting them. Refresh diagnostics/handles and explicitly review, retry or dispose those records before trying validation again.
6. Treat explicit disposal as analytics loss, never delivery success. Ordinary 7-day expiry/oldest eviction remain separate queue policy and do not clear a fault latch.

On withdrawal during recovery, abort local recovery through the consent gate and do not permit stale callbacks to clear the purge barrier. An already-started network probe remains best-effort cancellable only.

## 8. Verification checklist for a new game

- Confirm target Unity version and package graph; compile the copied SDK and renamed template without editing SDK source.
- Validate app ID, `device.platform`, explicit environment, endpoint, key mapping and TLS in non-production; never place a live mobile key in tests/source control.
- Test agreed event payloads against the game's approved contract and, optionally, Collector Pydantic schema. Verify all required fields and null/empty behavior.
- Test committed trigger points, exactly-once local triggering per game transition, save/resume IDs, event order, historical snapshots, sequence ownership and nested object immutability.
- Test fresh consent required after restart, queue replay only after grant, withdrawal purge/session reset/identity retention, already-started request race, and game-owned attempt state unchanged.
- Test queue bounds, oldest eviction, expiry, process termination/restart, storage failure/corrupt store, max body/count, retries, 413 split/single quarantine, 400/422 pause, explicit handle disposal/release/validation, stale revision and crash-recovery receipts.
- Build Android and iOS with the target scripting backend (including IL2CPP when used), run on both devices, test offline->restart->reconnect, and measure frame time, memory and physical storage overhead. Editor mocks do not prove mobile behavior.
- Report unsupported tests/targets as not run. Keep game-specific test evidence with the game project, not in this generic SDK.
