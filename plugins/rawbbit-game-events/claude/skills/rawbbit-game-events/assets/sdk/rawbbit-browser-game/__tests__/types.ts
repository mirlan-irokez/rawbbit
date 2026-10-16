import {
  createBrowserGameClient,
  BrowserGameError,
  type BrowserGameClient,
  type BrowserGameConfig,
  type BrowserGameDiagnostics,
  type EventInput,
  type EventContextMapping,
  type JsonValue,
  type QuarantineHandle,
  type RevocationStore,
  type SafeDiagnosticCode,
  createLocalStorageRevocationStore,
} from "../index.js";

const hostRevocationState: { token: string | null } = { token: null };
const revocationStore: RevocationStore = {
  readPending: () => hostRevocationState.token,
  markPending: () => (hostRevocationState.token = crypto.randomUUID()),
  clearPending: (token) => {
    if (hostRevocationState.token !== token) return false;
    hostRevocationState.token = null;
    return true;
  },
};

const config: BrowserGameConfig = {
  collectorUrl: "https://collector.example.test",
  apiKey: "app-scoped-public-key",
  appId: "game-example",
  environment: "prod",
  revocationStore,
  product: "example",
  surface: "browser_game",
  sourceType: "game_client",
  schemaVersion: 1,
  defaultDevice: { platform: "web", appVersion: "1.0", locale: "en", custom: [1, null] },
  legacyIdentityImport: {
    pseudoId: "approved-legacy-id",
    appId: "game-example",
    environment: "prod",
    policyVersion: "reviewed_v1",
  },
  clock: () => 1700000000000,
  random: () => 0.5,
  runtime: {
    window: {} as Window,
    document,
    indexedDB,
    locks: navigator.locks,
    crypto,
    fetch,
    BroadcastChannel: null,
    isSecureContext: true,
  },
  fetchImpl: fetch,
  autoFlush: false,
  maxQueueBytes: 10485760,
  retentionDays: 7,
  sessionTimeoutMs: 1800000,
  maxEventsPerRequest: 500,
  maxRequestBytes: 1048576,
  timeoutMs: 10000,
  maxRetries: 2,
  retryCooldownMs: 60000,
  backoffMs: 250,
  maxPendingTrackOperations: 64,
  maxPendingTrackBytes: 2097152,
  maxTrackBatchEvents: 500,
  lockTimeoutMs: 3000,
  storageTimeoutMs: 3000,
  flushTimeoutMs: 15000,
  maxResponseBytes: 65536,
  maxSnapshotDepth: 32,
  onPermissionRequired: () => {},
  sessionStartFactory(context, snapshot) {
    const capturedTime: string = snapshot.eventTimestamp;
    // @ts-expect-error Hook snapshots are deeply readonly.
    snapshot.eventName = "changed";
    // @ts-expect-error Context is immutable.
    context.eventSequence = 5;
    // @ts-expect-error Nested hook JSON is immutable.
    if (snapshot.eventParams) snapshot.eventParams.changed = true;
    return {
      eventName: "session_started",
      eventTimestamp: capturedTime,
      eventParams: { session: context.sessionId, sequence: context.sessionStartedEventSequence },
    };
  },
  eventContextMapper(context, snapshot) {
    return {
      event_params: {
        sequence: context.eventSequence,
        trigger: context.triggeringEventSequence,
        newSession: context.isNewSession,
        timestamp: context.eventTimestamp,
        name: snapshot.eventName,
      },
      userProperties: { approved: true },
      trafficSource: { source: "game", custom: { nested: [null, 2] } },
      geo: { country_code: "US", city: null },
      device: { app_version: "1.0", locale: "en" },
    };
  },
};

const camelEvent: EventInput = {
  eventName: "level_completed",
  eventId: "caller-id",
  eventTimestamp: "2026-10-13T12:00:00Z",
  eventParams: { score: 42, nested: [{ flag: true }, null] },
  user: { userId: "approved-account", extra: { tier: 2 } },
  consent: { analyticsStorage: true, adsStorage: null },
};
const snakeEvent: EventInput = {
  event_name: "level_started",
  event_id: null,
  event_timestamp: null,
  event_params: { chapter: 1 },
  user_properties: { approved: false },
  traffic_source: { medium: "direct" },
  device: { platform: "web", device_model: null },
  consent: { analytics_storage: true, ads_storage: false },
};

async function validUsage(): Promise<void> {
  const localStore: RevocationStore = createLocalStorageRevocationStore({ appId: "game-example", environment: "prod" });
  void localStore;
  const client: BrowserGameClient = await createBrowserGameClient(config);
  const localConsent: boolean = client.hasCurrentConsent;
  void localConsent;
  const confirmation = await client.confirmConsent(() => true);
  if (confirmation.safeDiagnosticCode === "withdrawalIncomplete") {
    await client.withdrawConsent();
  }
  await client.confirmConsent(async () => false);
  const tracked = await client.track(camelEvent);
  const safeCode: SafeDiagnosticCode = tracked.safeDiagnosticCode;
  void safeCode;
  if (tracked.status === "durablyStored") {
    const ids: readonly string[] = tracked.eventIds;
    const count: number = tracked.count;
    const prefix: string | undefined = tracked.prefixEventId;
    void [ids, count, prefix];
  } else {
    // @ts-expect-error Failed admission has no committed event IDs.
    tracked.eventIds;
  }
  await client.trackBatch([camelEvent, snakeEvent] as const);
  const delivery = await client.flush({ timeoutMs: 1000 });
  if (delivery.status === "sent") {
    const accepted: number | undefined = delivery.acceptedCount;
    void accepted;
  }
  const diagnostics: BrowserGameDiagnostics = await client.getDiagnostics();
  const handles: readonly QuarantineHandle[] = (await client.listQuarantined()).map(record => record.handle);
  const revisions = {
    expectedFaultRevision: diagnostics.faultRevision,
    expectedQueueRevision: diagnostics.queueRevision,
    policyVersion: "reviewed_v1",
  };
  await client.retryQuarantined(handles, { ...revisions, reasonCode: "configurationCorrected" });
  await client.discardQuarantined(handles, { ...revisions, reasonCode: "invalidFrozenEvent" });
  await client.validateAndResume({ ...revisions, reasonCode: "postDisposalValidation" });
  const withdrawal = await client.withdrawConsent();
  if ("globalWithdrawalCommitted" in withdrawal) {
    if (withdrawal.globalWithdrawalCommitted) {
      const removed: number = withdrawal.removed;
      void removed;
    } else {
      const disabled: true = withdrawal.localDisabled;
      void disabled;
    }
  }
  const disposed: void = client.dispose();
  void disposed;
}
void validUsage;

const error: Error = new BrowserGameError("unsupportedBrowser");
const errorCode: string = new BrowserGameError("storageFailure").code;
void [error, errorCode];

// @ts-expect-error Explicit environment is mandatory.
createBrowserGameClient({ collectorUrl: "https://example.test", apiKey: "key", appId: "game" });
// @ts-expect-error App IDs are strings, not numeric identifiers.
createBrowserGameClient({ ...config, appId: 123 });
// @ts-expect-error URI configuration uses strings, not URL objects.
createBrowserGameClient({ ...config, collectorUrl: new URL("https://example.test") });
// @ts-expect-error Legacy imports require app/environment/policy provenance.
createBrowserGameClient({ ...config, legacyIdentityImport: { pseudoId: "id" } });
// @ts-expect-error Hooks must return synchronously.
createBrowserGameClient({ ...config, sessionStartFactory: async () => ({ eventName: "session" }) });
// @ts-expect-error Mapper hooks must return synchronously.
createBrowserGameClient({ ...config, eventContextMapper: async () => ({ eventParams: {} }) });
// @ts-expect-error JSON cannot contain BigInt. Finiteness of number is checked at runtime.
const bigintJson: JsonValue = BigInt(1);
// @ts-expect-error Date is not plain JSON.
const dateJson: JsonValue = new Date();
// @ts-expect-error DOM nodes are not JSON.
const domJson: JsonValue = document.body;
// @ts-expect-error JSON cannot contain undefined.
const undefinedJson: JsonValue = { value: undefined };
// @ts-expect-error JSON cannot contain functions.
const functionJson: JsonValue = { callback: () => true };
// @ts-expect-error Known user identity attributes must be strings or null.
const invalidUser: EventInput = { eventName: "test", user: { userId: 5 } };
// @ts-expect-error Event names are required, using either supported alias.
const missingName: EventInput = { eventParams: {} };
// @ts-expect-error Device platform is SDK-owned web, not mobile.
const invalidPlatform: EventInput = { eventName: "test", device: { platform: "ios" } };
// @ts-expect-error Timestamps are RFC3339 strings, not Dates.
const invalidTime: EventInput = { eventName: "test", eventTimestamp: new Date() };
// @ts-expect-error Analytics storage cannot be denied on an admitted event.
const invalidConsent: EventInput = { eventName: "test", consent: { analytics_storage: false } };
// @ts-expect-error Mapper cannot modify user identity.
const identityMapping: EventContextMapping = { eventParams: {}, user: { userId: "replacement" } };
// @ts-expect-error Mapper cannot modify top-level userId either.
const userIdMapping: EventContextMapping = { eventParams: {}, userId: "replacement" };
// @ts-expect-error Mapper cannot modify platform.
const platformMapping: EventContextMapping = { device: { platform: "web" } };
// @ts-expect-error Mapper cannot modify SDK-owned timestamps.
const timeMapping: EventContextMapping = { eventTimestamp: "2026-10-13T12:00:00Z" };
// @ts-expect-error Collector-owned app ID is not an event input field.
const appMapping: EventInput = { eventName: "test", appId: "other-game" };
void [bigintJson, dateJson, domJson, undefinedJson, functionJson, invalidUser, missingName,
  invalidPlatform, invalidTime, invalidConsent, identityMapping, userIdMapping, platformMapping,
  timeMapping, appMapping];

function invalidUsage(client: BrowserGameClient, handles: readonly QuarantineHandle[]): void {
  // @ts-expect-error BrowserGameClient is a type, not an exported constructor.
  new BrowserGameClient(config);
  // @ts-expect-error Permission providers must resolve booleans.
  client.confirmConsent(async () => "yes");
  // @ts-expect-error Recovery requires both expected revisions and policy version.
  client.validateAndResume({ reasonCode: "configurationCorrected" });
  // @ts-expect-error Caller event IDs are not opaque queue handles.
  client.retryQuarantined(["event-id"], { expectedFaultRevision: 1, expectedQueueRevision: 1, reasonCode: "sdkFixValidated", policyVersion: "v1" });
  // @ts-expect-error Discard reasons cannot authorize a retry.
  client.retryQuarantined(handles, { expectedFaultRevision: 1, expectedQueueRevision: 1, reasonCode: "invalidFrozenEvent", policyVersion: "v1" });
  // @ts-expect-error Retry reasons cannot authorize a discard.
  client.discardQuarantined(handles, { expectedFaultRevision: 1, expectedQueueRevision: 1, reasonCode: "configurationCorrected", policyVersion: "v1" });
  // @ts-expect-error Resume reasons are a fixed enum.
  client.validateAndResume({ expectedFaultRevision: 1, expectedQueueRevision: 1, reasonCode: "force", policyVersion: "v1" });
}
void invalidUsage;
