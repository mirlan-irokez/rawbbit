# Rawbbit Unity SDK (copy-in source)

Generic Unity client source for sending game-owned events to the Rawbbit Collector. The runtime has no game-specific event catalogue or gameplay-state dependency. Each game supplies its own analytics adapter using [`INTEGRATION.md`](INTEGRATION.md) and the starter in `Examples/GameAnalyticsTemplate/`.

## Status

Initial source implementation. **Unity Editor compilation and Android/iOS player/device validation have not been run in this environment.** Unity 2022.3 LTS is the provisional API baseline; confirm the consuming project's `ProjectSettings/ProjectVersion.txt` and pass the integration gates before claiming a supported production build.

## Install in a game

1. Copy `Runtime/` into the game at `Assets/Rawbbit/`.
2. Install Unity's Newtonsoft JSON package through Package Manager: `com.unity.nuget.newtonsoft-json`. The package supplies `Newtonsoft.Json.dll`, which Unity references automatically because the SDK asmdef leaves precompiled references enabled. No exact package version is pinned because the target editor is not available here; resolve and record a compatible version in the consuming project before release. Do not copy another Newtonsoft DLL into `Assets`.
3. Copy and rename `Examples/GameAnalyticsTemplate/GameAnalyticsAdapter.cs.template` into the game's own analytics folder (for example `Assets/Game/Analytics/GameAnalyticsAdapter.cs`) and change its namespace/configuration/event mapping.
4. Follow [`INTEGRATION.md`](INTEGRATION.md). Do not edit SDK files to add a game's events.

The copy-in tree is not a published UPM package. Preserve the SDK source revision used by each game and update deliberately.

## Initialization and event tracking

The host creates one persistent Unity object that pumps one sender across scenes:

```csharp
using System.Collections.Generic;
using System.Threading.Tasks;

var host = RawbbitUnityHost.Create(new RawbbitConfiguration {
    CollectorUrl = runtime.CollectorUrl,
    ApiKey = runtime.AppScopedApiKey,
    AppId = "your.game",                 // same ID on Android and iOS
    Environment = runtime.Environment,    // set explicitly; query the same value
    Product = "your_product",
    Surface = "unity_game",
    Platform = null                       // host selects android/ios/unity_editor/unity_other
});
while (!host.IsReady && !host.InitializationFailed) await Task.Yield();
if (host.InitializationFailed) throw new System.InvalidOperationException("Rawbbit initialization failed.");
await host.ConfirmConsentAsync();         // only after the game's policy grants permission
RawbbitTrackResult result = await host.TrackAsync(new RawbbitEventInput {
    EventName = "match_completed",        // belongs to the game, not Rawbbit SDK
    EventParams = new Dictionary<string, object> { ["score"] = 125 },
    UserProperties = capturedSnapshot
});
```

`RawbbitUnityHost` exposes `IsReady`, `InitializationFailed`, and `Client`; wait for `IsReady` before using `Client` or the forwarding methods. Use the game's async lifecycle/bootstrap pattern rather than blocking the Unity main thread.

`TrackAsync` snapshots caller data synchronously and persists the immutable envelope on a worker task. `DurablyStored` means the local queue commit completed; it does not mean the Collector or ClickHouse accepted it. Callers should observe storage/validation outcomes but must not wait for network delivery in a gameplay transition.

Call `RawbbitUnityHost.Create` on Unity's main thread: it reads Unity platform/version/system information synchronously. The host sets `device.platform` to `android` or `ios` in player builds, `unity_editor` in the Editor, and `unity_other` on other targets; it captures `app_version`, `os_version`, and `device_model` defaults, which caller-configured defaults and per-event values may override (including explicit `null`). The SDK always owns `device.platform`. It does not infer the game's selected locale or collect hardware identifiers. Keep environment selection explicit so Editor/test events do not silently appear in production queries. The app key is extractable from a mobile build; scope it to the game's `app_id`, deliver it through game runtime configuration, and plan rotation. It is not a secret. The request sets `redirectLimit = 0` to refuse redirects; verify this behavior on the target Unity version before release.

## Runtime behavior and defaults

| Setting | Default | Valid range |
| --- | ---: | ---: |
| `MaxQueueBytes` | 10 MiB | 1 KiB–100 MiB |
| `RetentionDays` | 7 | 1–365 days |
| `SessionTimeoutMinutes` | 30 | 1–1,440 minutes |
| `MaxEventsPerRequest` | 500 | 1–500 |
| `MaxRequestBytes` | 1 MiB | 512 bytes–1 MiB |
| `RequestTimeoutSeconds` | 10 | 1–120 seconds |
| `MaxImmediateRetries` | 2 | 0–10 |
| `RetryCooldownSeconds` | 60 | 1–3,600 seconds |
| `JournalCompactionBytes` | 1 MiB | 64 KiB–8 MiB |
| `MaxPendingTrackOperations` | 64 | 1–64 |
| `AllowInsecureHttpForDevelopment` | `false` | explicit opt-in only |
| `AllowFreshStoreWithUnresolvedLegacyStores` | `false` | explicit host assertion; see persistent-store notes |

Other configuration values (`CollectorUrl`, `ApiKey`, `AppId`, `Environment`) are required; `Platform` is set by the Unity host. `PreviousCollectorUrl` is optional, identifies the old URL for an authorized endpoint change, and does not change the current request destination. It may be present in shared configuration for fresh installs; see the persistent-store notes for the default unresolved-legacy check and its explicit new-store assertion. `MaxEventsPerRequest` and `MaxRequestBytes` cannot exceed the Collector's defaults.

- Endpoint: `POST /v1/events:batch`, JSON `{ "events": [...] }`, header `X-API-Key`.
- Event fields: Rawbbit envelope (`event_id`, `app_id`, `environment`, `event_name`, `event_timestamp`, `user`, `device`, JSON `event_params` / `user_properties`, `traffic_source`, `geo`, and `consent.analytics_storage=true`). Collector-owned `received_at`, `ingest`, queue, network and NATS fields are not authored by this SDK.
- Collector context fields are checked against the current schema: user identifiers and device fields (`platform`, `app_version`, `os_version`, `device_model`, `locale`, `timezone`) are strings/null; traffic-source and geo context values are strings/null. `event_params` and `user_properties` remain arbitrary nested JSON.
- Caller owns event names, event-time properties, gameplay sequences, consent policy, account IDs and committed-transition timing. If no pseudo ID is supplied, the SDK's app-generated persistent installation ID is used. No hardware/ad IDs are read.
- `IRawbbitIdentityProvider` can supply an approved game account/pseudo-ID. An optional `IRawbbitSessionIdProvider` can supply session IDs, but it must return a fresh, non-empty, control-character-free value for every new session; the default provider generates UUIDs.
- SDK-managed session timeout defaults to **more than 30 minutes** of inactivity between permitted tracking calls. Exactly 30 minutes remains the same session. Tracking, not upload, updates the clock. The timeout is configurable.
- On a new session, an optional adapter factory may return a game-specific `session_started` event. Returning `null` omits the prefix and leaves the trigger at sequence 1; a returned prefix is durably queued before its trigger, with the same session ID and sequences 1/2. The synchronous factory runs on a worker thread and must be pure/Unity-API-independent. An optional `RawbbitEventContextMapper` runs for each tracked trigger (also on a worker thread) and receives the frozen snapshot plus session ID, new-session flag and generic trigger sequence so the adapter can map that sequence into its own schema. The SDK does not choose a game property name.
- Queue cap: **10 MiB logical record accounting** (frozen UTF-8 event JSON plus a 512-byte metadata reservation); oldest records, including quarantined ones, may be evicted to admit a new event. Session-start prefix and trigger are admitted atomically. Queue retention: **7 days from local enqueue**; expiry is processed at initialization, enqueue, active-process maintenance and before sending. Closed apps/backups can retain physical data longer.
- The local store is a checksummed append journal plus rotating checkpoints under the app's `Application.persistentDataPath`, in `Rawbbit/store-v2-<hash(AppId, Environment)>`. The stable directory key deliberately excludes the Collector URL. Snapshot/journal compaction creates bounded extra disk use; `MaxQueueBytes` applies to logical queue records, not transient checkpoint files. A completed consent withdrawal writes a payload-free checkpoint and removes SDK-managed prior journals/checkpoints/temp files before reporting purge success. OS backups can still retain older copies. This is not encrypted storage.
- Collector default request limits: **500 events / 1 MiB**. The SDK will not configure higher limits. It splits batches by actual UTF-8 JSON body size and count.
- Transport: UnityWebRequest, 10-second timeout, at most two immediate retries with exponential backoff and bounded jitter, then 60-second cooldown. Network errors, 408, 429 and 5xx retry; 401/403/404 and unexpected errors persist a sender fault and pause delivery. A 413 batch response reduces event-count batch size; a single-event 413 is quarantined.
- Immediately before transport starts, the worker-state and store gates use nonblocking `Monitor.TryEnter`; contention on those gates defers dispatch instead of waiting for disk work or adapter callbacks. The short consent gate remains serialized. Under the store lock, the SDK reads the clock and rechecks that each prepared record is still present and unchanged (handle, event ID, enqueue time and frozen payload), pending, and unexpired. It also checks store health, withdrawal/purge barriers and fault state; a recovery probe must still match the current fault revision and queue generation. The expiry check at this boundary is in-memory and does not prune disk. The ordinary sender drops a stale or deferred batch snapshot and prepares again on a later pump, without recording a network failure or retry penalty; a declined recovery probe requires another explicit recovery call. Normal dispatch remains gated by current consent.
- 400/422 responses quarantine every event in the rejected batch; a single-event 413 is quarantined and delivery pauses. Do not assume the response proves the SDK payload itself is bad; it could be a serialization or deployment configuration defect. The queue never auto-deletes a rejected record. Recovery is host-authorized and explicit: inspect opaque handles/fault revisions, release validated original records for retry or explicitly dispose selected quarantine records with a reason/policy, then validate and clear the fault. A latched fault with `RecoveryProbeRequired` (including server responses and local frozen-event validation faults) requires a consent-gated probe that sends exactly one already-persisted, validated event and clears only that exact record after the Collector reports `accepted_events: 1`; an empty queue returns a probe-pending result. This is never a synthetic analytics event or an automatic queue walk. Recovery operations return fixed diagnostic codes and do not log payloads.
- Consent is **unknown on every initialization**. The host must re-confirm current permission before tracking, replay, retries or recovery probes; an SDK's previous grant is not persisted as authorization. Unknown itself does not invoke withdrawal or authorize dispatch; normal expiry maintenance can remove expired records, and startup completes any previously committed purge before a later grant. Explicit withdrawal blocks subsequent dispatch and durably purges local pending/quarantined events, clears session ID/activity/counter, but retains installation identity. Re-consent starts a new session at the next permitted tracked event. An already-started request is only best-effort aborted and may still reach the Collector; server-accepted events cannot be recalled.
- Delivery is at least once. Event IDs and frozen payloads are retained across retries; ambiguous responses can lead to duplicates. The Collector's JetStream deduplication window is finite. Local enqueue is not proof of Collector acceptance or ClickHouse visibility.

## Consent responsibility

The SDK does not display a consent screen or determine whether analytics collection is permitted. 
The game owns its consent UI, privacy settings, and permission decision. 
Having no consent screen does not itself authorize collection.

Call `ConfirmConsentAsync()` only after the game has verified current analytics permission. 
Repeat this confirmation on every SDK initialization; this does not necessarily require showing a new screen on every launch. 
Sent events declare `consent.analytics_storage=true`.

Without confirmation, new tracking calls return `ConsentUnknown` and no events are uploaded. 
Previously queued events remain subject to normal retention. 
If permission is explicitly denied or withdrawn, call  `WithdrawConsentAsync()` to block tracking and purge local events.

## Persistent-store identity and endpoint migration

The new stable store directory is keyed by `AppId` and `Environment`, not by endpoint. The on-disk format remains version 1; the `CollectorEndpoint` binding and its transaction fields are additive. On an unambiguous migration, the SDK preserves the installation ID, session, queued and quarantined payloads, enqueue times/retention, and delivery-fault state. Normal retention processing still applies during startup.

- An upgrade using the same Collector URL automatically adopts the matching old URL-keyed directory. If the Collector URL changes during the first upgrade, set `PreviousCollectorUrl` to the old URL so the SDK can locate that store and explicitly authorize the change. Use this only for the same logical Collector app/environment and with the app-scoped key approved for the new destination; `CollectorUrl` and `ApiKey` continue to select the current destination.
- The fresh-store guard applies only when no stable, current-endpoint legacy, or `PreviousCollectorUrl` legacy store is selected. Existing selection, adoption, conflicts, selected-store corruption checks, and stored endpoint-binding enforcement are unchanged. A v1 legacy directory's hash includes the endpoint, app ID, and environment; without the old endpoint, the SDK cannot reliably establish that the selected app/environment has no v1 store at another URL. An unresolved legacy sibling therefore blocks fresh initialization by default and remains untouched. An unrelated stable sibling is recognized only when its directory basename is exactly `store-v2-` followed by 32 lowercase hexadecimal characters (`[0-9a-f]`).
- `AllowFreshStoreWithUnresolvedLegacyStores` defaults to `false`. Set it to `true` only when the host has independently verified that the selected app ID/environment is genuinely new and has no old v1 store at any unspecified endpoint. On the fresh-store path, the opt-in permits proceeding despite unresolved legacy sibling directories only when each such directory basename is exactly 32 lowercase hexadecimal characters (`[0-9a-f]`). Root-level files always block fresh creation. Directory siblings must either use the strict unrelated-stable name above or, with the opt-in enabled, the exact legacy-sibling name; every other directory basename blocks even with the opt-in enabled. These names are classification rules, not proof of v1 contents or unrelated ownership; the host's verified-new assertion is still required. The guard does not scan or deserialize sibling contents, and siblings remain untouched. Misuse can silently strand that app/environment's old queue and installation identity. This is not an endpoint-migration shortcut: for a known existing app/environment move, use `PreviousCollectorUrl` to identify the old endpoint. The flag changes local store initialization only, not the event schema, Collector API, or authentication behavior.
- A selected nonempty stable or legacy directory without a recognized snapshot or journal fails closed and remains untouched; it is not initialized as an empty queue. The sole exception is a directory containing only SDK-owned initial checkpoint temps (`snapshot-00000000000000000000.json.<32-digit GUID>.tmp`): these precede every identity/event commit and are removed to recover interrupted first initialization. Mixed unknown contents, invalid GUID names, and later-checkpoint temps remain protected. A stable store alongside a known legacy root, or multiple known legacy roots, also fails closed: stores are not merged or deleted.
- For a legacy source, the endpoint binding is committed in that directory before it is renamed to the stable directory. A crash before or after the rename therefore retains the binding and queue state. An actual endpoint change retains any existing fault; for a probe-required fault, it clears prior probe validation and revises the fault token, requiring a fresh probe for the new destination. Non-probe faults retain their revision and do not acquire a probe requirement. Endpoint migration does not grant consent or clear a delivery fault; re-confirm current consent and use the explicit recovery flow for a fault.
- Downgrading to an SDK build that uses the old URL-keyed store layout is unsupported: it opens a different path and will not see data in the new stable store. Do not copy or merge store directories to roll back.

## Recovery and diagnostics

Poll `client.GetDiagnostics()` for logical queue bytes, pending/quarantine counts, current-client locally rejected input count, cumulative expiry/eviction/explicit-disposal counts (including separate quarantined expiry/eviction counters), sender fault code, queue generation and fault revision. `LocallyRejectedCount` is in-memory for this client instance; queue loss/fault counters are checkpointed with the local store. `client.ListQuarantinedRecords()` returns opaque record handles and safe HTTP/fault/time metadata, not payloads or user identifiers.

After correcting and reviewing the cause, recovery callers should use current `FaultRevision` and `QueueGeneration` from diagnostics. The retry, disposal, validation, and probe operations all require fresh current consent; read-only diagnostics/quarantine listing do not. Explicitly retry selected records only when the original frozen bytes are valid (`configuration_corrected`, `collector_limit_corrected`, or `sdk_fix_validated`); explicitly dispose selected handles only with the fixed reason codes `invalid_frozen_event`, `collector_contract_rejected`, or `unsupported_frozen_payload`. Then, under current consent and from Unity's main thread, call `RawbbitUnityHost.ValidateAndResumeAsync` with `configuration_corrected`, `collector_limit_corrected`, `sdk_fix_validated`, or `post_disposal_validation`. It obtains one bounded probe whenever the latched fault's `RecoveryProbeRequired` flag is set, including local validation faults. Probe HTTP 400/413/422 re-quarantines that event; network errors, 408/429/5xx, and accepted-count mismatches leave it pending. Other non-retryable responses (for example 401/403/404 or a redirect response) also leave it pending and keep the fault latched. If validation finds invalid pending records, it quarantines them in bounded durable chunks for explicit review; inspect updated handles/revisions and explicitly retry or dispose them before validating again. A stale revision, unresolved quarantine, absent valid probe event, invalid frozen payload, storage/purge problem or unknown store version leaves sending paused. A fresh consent confirmation cannot clear the fault.

Withdrawal and data loss are surfaced through returned `RawbbitRecoveryResult` / `RawbbitTrackResult` codes and diagnostics, without logging event bodies, API keys, player/session IDs or response bodies. If local storage fails during withdrawal, the host remains disabled; do not claim purge succeeded.

## Validation

The source includes Editor unit tests and PlayMode lifecycle tests. In a disposable Unity project with the package installed:

```bash
"$UNITY_EDITOR" -batchmode -runTests -projectPath "$UNITY_TEST_PROJECT" \
  -testPlatform EditMode -testResults "$RESULT_DIR/editmode.xml"
"$UNITY_EDITOR" -batchmode -runTests -projectPath "$UNITY_TEST_PROJECT" \
  -testPlatform PlayMode -testResults "$RESULT_DIR/playmode.xml"
```

Then build/run on Android and iOS. Check offline enqueue, force-stop/restart with consent unknown, fresh consent and replay, eviction/expiry, quarantine/recovery, withdrawal races, scene transitions, IL2CPP and storage/frame costs. An iOS Xcode export is not an iOS device test. See `INTEGRATION.md` for the full matrix. No Unity executable was available when these source files were prepared, so no Unity test is claimed as passing.
