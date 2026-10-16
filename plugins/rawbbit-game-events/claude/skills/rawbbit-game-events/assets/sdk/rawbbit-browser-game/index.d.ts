/** Plain, acyclic JSON only. Numbers must be finite; runtime validates these constraints. */
export type JsonValue = null | boolean | number | string | JsonObject | readonly JsonValue[];
export interface JsonObject {
  readonly [key: string]: JsonValue;
}

export type UserContext = JsonObject & {
  readonly userId?: string | null;
  readonly user_id?: string | null;
  readonly userPseudoId?: string | null;
  readonly user_pseudo_id?: string | null;
  /** SDK-owned; if supplied, must match the candidate session ID. */
  readonly sessionId?: string | null;
  readonly session_id?: string | null;
};

export type DeviceProperties = JsonObject & {
  readonly appVersion?: string | null;
  readonly app_version?: string | null;
  readonly osVersion?: string | null;
  readonly os_version?: string | null;
  readonly deviceModel?: string | null;
  readonly device_model?: string | null;
  readonly locale?: string | null;
  readonly timezone?: string | null;
};
export type DeviceContext = DeviceProperties & { readonly platform?: "web" | null };
export type MappedDeviceContext = DeviceProperties & { readonly platform?: never };
export type TrafficSource = JsonObject & {
  readonly source?: string | null;
  readonly medium?: string | null;
  readonly campaign?: string | null;
};
export type GeoContext = JsonObject & {
  readonly country?: string | null;
  readonly countryCode?: string | null;
  readonly country_code?: string | null;
  readonly city?: string | null;
  readonly region?: string | null;
  readonly timezone?: string | null;
};
export type EventConsent = JsonObject & {
  readonly analyticsStorage?: true;
  readonly analytics_storage?: true;
  readonly adsStorage?: boolean | null;
  readonly ads_storage?: boolean | null;
  readonly personalization?: boolean | null;
};

export interface EventFields {
  readonly eventId?: string | null;
  readonly event_id?: string | null;
  /** RFC3339 string, not a Date. Omitted timestamps are captured at track call time. */
  readonly eventTimestamp?: string | null;
  readonly event_timestamp?: string | null;
  readonly user?: UserContext | null;
  readonly device?: DeviceContext | null;
  readonly eventParams?: JsonObject | null;
  readonly event_params?: JsonObject | null;
  readonly userProperties?: JsonObject | null;
  readonly user_properties?: JsonObject | null;
  readonly trafficSource?: TrafficSource | null;
  readonly traffic_source?: TrafficSource | null;
  readonly geo?: GeoContext | null;
  readonly consent?: EventConsent | null;
}
export type EventInput = EventFields & (
  | { readonly eventName: string; readonly event_name?: string }
  | { readonly eventName?: string; readonly event_name: string }
);

/** Detached, deeply immutable input supplied to synchronous adapter hooks. */
export type EventSnapshot = EventInput & {
  readonly eventName: string;
  readonly eventId: string;
  readonly eventTimestamp: string;
};
export interface EventContext {
  readonly sessionId: string;
  readonly isNewSession: boolean;
  /** This event's committed session counter: prefix 1, first trigger 2. */
  readonly eventSequence: number;
  /** 1 when this admission includes a prefix; otherwise 0. */
  readonly sessionStartedEventSequence: number;
  /** Current trigger's counter; for a prefix context, the first trigger is 2. */
  readonly triggeringEventSequence: number;
  readonly eventTimestamp: string;
}

/** Only game-owned property/context fields may be returned by the mapper. */
export interface EventContextMapping {
  readonly eventParams?: JsonObject | null;
  readonly event_params?: JsonObject | null;
  readonly userProperties?: JsonObject | null;
  readonly user_properties?: JsonObject | null;
  readonly trafficSource?: TrafficSource | null;
  readonly traffic_source?: TrafficSource | null;
  readonly geo?: GeoContext | null;
  readonly device?: MappedDeviceContext;
  readonly user?: never;
  readonly userId?: never;
  readonly user_id?: never;
  readonly sessionId?: never;
  readonly session_id?: never;
  readonly eventId?: never;
  readonly event_id?: never;
  readonly eventTimestamp?: never;
  readonly event_timestamp?: never;
  readonly eventName?: never;
  readonly event_name?: never;
  readonly appId?: never;
  readonly app_id?: never;
  readonly environment?: never;
  readonly consent?: never;
  readonly platform?: never;
}

export interface LegacyIdentityImport {
  readonly pseudoId: string;
  readonly appId: string;
  readonly environment: string;
  readonly policyVersion: string;
}

/** Browser test seams, not a supported Node/server runtime. */
export interface BrowserRuntime {
  readonly window?: Window;
  readonly document?: Document;
  readonly indexedDB?: IDBFactory;
  readonly locks?: LockManager;
  readonly crypto?: Crypto;
  readonly fetch?: typeof fetch;
  readonly BroadcastChannel?: typeof BroadcastChannel | null;
  readonly isSecureContext?: boolean;
}

/**
 * Host-persisted, endpoint-independent pending-withdrawal journal. Its state
 * must survive SDK client/page restart and be committed independently of the
 * event queue. Methods run under the SDK namespace Web Lock and must be bounded.
 */
export interface RevocationStore {
  /** Return null only when no pending revocation is durably recorded. */
  readPending(): string | null | Promise<string | null>;
  /** Durably replace the marker with a fresh opaque token and return that token. */
  markPending(): string | Promise<string>;
  /**
   * Atomically clear only when the current marker equals token. Return true
   * only after durable commit; false/rejection must leave the marker intact.
   */
  clearPending(token: string): boolean | Promise<boolean>;
}

export interface LocalStorageRevocationStoreOptions {
  readonly appId: string;
  readonly environment: string;
  /** Defaults to same-origin localStorage; storage errors fail closed. */
  readonly storage?: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  /** Test/host injection; must provide secure random ID generation. */
  readonly crypto?: Crypto;
}

export interface BrowserGameConfig {
  /** Absolute HTTPS URI without credentials, query or fragment. */
  readonly collectorUrl: string;
  readonly apiKey: string;
  readonly appId: string;
  readonly environment: string;
  /** Required persistence separate from IndexedDB; see RevocationStore. */
  readonly revocationStore: RevocationStore;
  readonly previousCollectorUrl?: string;
  readonly allowInsecureHttpForDevelopment?: boolean;
  readonly product?: string;
  /** @default "browser_game" */
  readonly surface?: string;
  /** @default "game_client" */
  readonly sourceType?: string;
  /** @default 1 */
  readonly schemaVersion?: number | string;
  readonly defaultDevice?: DeviceContext;
  readonly legacyIdentityImport?: LegacyIdentityImport;
  /** @default true */
  readonly autoFlush?: boolean;
  /** Finite, nonnegative epoch milliseconds. Defaults to Date.now. */
  readonly clock?: () => number;
  /** Retry jitter test seam; does not generate event or installation IDs. */
  readonly random?: () => number;
  readonly runtime?: BrowserRuntime;
  readonly fetchImpl?: typeof fetch;
  readonly onPermissionRequired?: (() => void) | null;
  /** Pure, synchronous, non-reentrant; may be recomputed on a session revision conflict. */
  readonly sessionStartFactory?: ((context: EventContext, triggeringSnapshot: EventSnapshot) => EventInput | null | undefined) | null;
  /** Pure, synchronous, non-reentrant; cannot override SDK-owned fields. */
  readonly eventContextMapper?: ((context: EventContext, eventSnapshot: EventSnapshot) => EventContextMapping | null | undefined) | null;
  /** @default 10485760 */
  readonly maxQueueBytes?: number;
  /** Finite positive duration. @default 7 */
  readonly retentionDays?: number;
  /** @default 1800000 */
  readonly sessionTimeoutMs?: number;
  /** Integer 1..500. @default 500 */
  readonly maxEventsPerRequest?: number;
  /** Integer 1..1048576. @default 1048576 */
  readonly maxRequestBytes?: number;
  /** Full request/body deadline. @default 10000 */
  readonly timeoutMs?: number;
  /** Nonnegative safe integer. @default 2 */
  readonly maxRetries?: number;
  /** @default 60000 */
  readonly retryCooldownMs?: number;
  /** @default 250 */
  readonly backoffMs?: number;
  /** @default 64 */
  readonly maxPendingTrackOperations?: number;
  /** @default 2097152 */
  readonly maxPendingTrackBytes?: number;
  /** Integer 1..500 input events; a prefix is additional. @default 500 */
  readonly maxTrackBatchEvents?: number;
  /** @default 3000 */
  readonly lockTimeoutMs?: number;
  /** @default 3000 */
  readonly storageTimeoutMs?: number;
  /** @default 15000 */
  readonly flushTimeoutMs?: number;
  /** @default 65536 */
  readonly maxResponseBytes?: number;
  /** Integer 1..128. @default 32 */
  readonly maxSnapshotDepth?: number;
}

export type SafeDiagnosticCode =
  | "durablyStored" | "consentUnknown" | "consentWithdrawn" | "busy"
  | "invalidEvent" | "storageFailure" | "unsupportedBrowser"
  | "callbackReentrancy" | "permissionCheckFailed" | "consentChanged"
  | "consentConfirmed" | "withdrawalCommitted" | "withdrawalIncomplete" | "disposed"
  | "revocationStorageFailure" | "revocationStorageInvalid" | "revocationClearPending"
  | "operationTooLarge" | "sessionRevisionChanged" | "consentOrLifecyclePaused"
  | "offline" | "deliveryFaultPaused" | "retryCooldown" | "empty"
  | "probeEventRequired" | "invalidFrozenEvent" | "singleEventTooLarge"
  | "stalePreparation" | "requestCancelled" | "staleCompletion"
  | "staleRecovery" | "unresolvedQuarantine" | "probeRecordChanged"
  | "collectorAccepted" | "acceptedCountMismatch" | "retryScheduled"
  | "batchReduced" | "flushDeadline" | "invalidRecoverySelection"
  | "selectionCommitted" | "recoveryPending" | "invalidRecoveryPolicy" | "resumed";

export interface OperationResult<Status extends string> {
  readonly status: Status;
  readonly safeDiagnosticCode: SafeDiagnosticCode;
}
export type TrackResult =
  | (OperationResult<"durablyStored"> & {
      readonly eventIds: readonly string[];
      readonly count: number;
      readonly prefixEventId?: string;
    })
  | OperationResult<"consentUnknown" | "consentWithdrawn" | "busy" | "invalidEvent" | "storageFailure" | "unsupportedBrowser">;

export type WithdrawalResult =
  | (OperationResult<"withdrawn"> & {
      readonly removed: number;
      readonly globalWithdrawalCommitted: true;
      readonly revocationCleared: boolean;
      readonly localDisabled: boolean;
    })
  | (OperationResult<"busy" | "consentUnknown" | "consentWithdrawn" | "storageFailure"> & {
      readonly globalWithdrawalCommitted: false;
      readonly revocationCleared: boolean;
      readonly localDisabled: true;
    })
  | OperationResult<"invalidEvent">;
export type ConsentResult = WithdrawalResult | OperationResult<"confirmed" | "consentUnknown" | "busy">;

export interface FlushOptions {
  /** Finite positive duration up to 60000 ms. Defaults to configured flushTimeoutMs. */
  readonly timeoutMs?: number;
}
export type DeliveryResult =
  | (OperationResult<"sent"> & { readonly acceptedCount?: number })
  | OperationResult<"empty" | "paused" | "offline" | "busy" | "timeout" | "pending" | "invalidEvent" | "storageFailure" | "consentUnknown" | "consentWithdrawn">;

export interface LossCounters {
  readonly expired: number;
  readonly evicted: number;
  readonly quarantinedExpired: number;
  readonly quarantinedEvicted: number;
  readonly discarded: number;
}
export interface BrowserGameDiagnostics {
  /** This instance owns the sender lock; peers may separately own it. */
  readonly senderActive: boolean;
  readonly lastBusy: boolean;
  /** Retry withdrawal successfully before this instance can grant again. */
  readonly withdrawalIncomplete: boolean;
  readonly revocationStorageCode: "revocationStorageFailure" | "revocationStorageInvalid" | "revocationClearPending" | null;
  readonly consent: "unknown" | "granted" | "withdrawn";
  readonly storageAvailable: boolean;
  readonly storageCode: string | null;
  readonly pendingOperations: number;
  readonly pendingBytes: number;
  readonly pendingCount: number;
  readonly quarantinedCount: number;
  readonly queueBytes: number;
  readonly queueRevision: number;
  readonly counters: LossCounters | null;
  readonly faultCode: string | null;
  readonly faultRevision: number;
  readonly recoveryProbeRequired: boolean;
  readonly recoveryProbeValidated: boolean;
  readonly retryNotBefore: number;
  readonly retryAttempt: number;
  readonly lastHttpStatus: number | null;
}

declare const quarantineHandleBrand: unique symbol;
/** Opaque local queue handle; not an event ID. Obtain from listQuarantined. */
export type QuarantineHandle = string & { readonly [quarantineHandleBrand]: true };
export interface QuarantinedRecord {
  readonly handle: QuarantineHandle;
  readonly httpStatus: number;
  readonly faultCode: string;
  readonly enqueuedAt: number;
}
export type RetryReasonCode = "configurationCorrected" | "collectorLimitCorrected" | "sdkFixValidated";
export type DiscardReasonCode = "invalidFrozenEvent" | "collectorContractRejected" | "unsupportedFrozenPayload";
export type ResumeReasonCode = RetryReasonCode | "postDisposalValidation";
export interface RecoveryOptions<Reason extends string> {
  readonly expectedFaultRevision: number;
  readonly expectedQueueRevision: number;
  readonly reasonCode: Reason;
  /** Reviewed policy identifier: 1..64 ASCII letters, digits, underscores, periods or hyphens. */
  readonly policyVersion: string;
}
export type RetryQuarantinedOptions = RecoveryOptions<RetryReasonCode>;
export type DiscardQuarantinedOptions = RecoveryOptions<DiscardReasonCode>;
export type ValidateAndResumeOptions = RecoveryOptions<ResumeReasonCode>;
export type RecoveryResult =
  | (OperationResult<"recovered"> & { readonly affected: number })
  | OperationResult<"invalidEvent" | "busy" | "consentUnknown" | "consentWithdrawn" | "recoveryPending">;
export type ResumeResult =
  | (OperationResult<"resumed"> & { readonly acceptedCount?: number })
  | OperationResult<"invalidEvent" | "busy" | "consentUnknown" | "consentWithdrawn" | "recoveryPending" | "paused" | "pending">;

/** Created only by createBrowserGameClient; there is no exported client constructor. */
export interface BrowserGameClient {
  /** Local capability getter, not a fresh durable cross-tab permission check. */
  readonly hasCurrentConsent: boolean;
  track(event: EventInput): Promise<TrackResult>;
  trackBatch(events: readonly EventInput[]): Promise<TrackResult>;
  confirmConsent(permissionProvider: () => boolean | Promise<boolean>): Promise<ConsentResult>;
  withdrawConsent(): Promise<WithdrawalResult>;
  flush(options?: FlushOptions): Promise<DeliveryResult>;
  getDiagnostics(): Promise<BrowserGameDiagnostics>;
  listQuarantined(): Promise<readonly QuarantinedRecord[]>;
  retryQuarantined(handles: readonly QuarantineHandle[], options: RetryQuarantinedOptions): Promise<RecoveryResult>;
  discardQuarantined(handles: readonly QuarantineHandle[], options: DiscardQuarantinedOptions): Promise<RecoveryResult>;
  validateAndResume(options: ValidateAndResumeOptions): Promise<ResumeResult>;
  dispose(): void;
}

export class BrowserGameError extends Error {
  constructor(code: string);
  readonly code: string;
}

/** Validates a top-level secure browser and durable storage; starts with unknown consent. */
export function createBrowserGameClient(config: BrowserGameConfig): Promise<BrowserGameClient>;
export function createLocalStorageRevocationStore(options: LocalStorageRevocationStoreOptions): RevocationStore;
