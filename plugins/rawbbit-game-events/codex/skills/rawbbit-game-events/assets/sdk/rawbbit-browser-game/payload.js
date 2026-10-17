import { buildCollectorEvent } from "./vendor/rawbbit-client/normalize.js"

const MAX_SNAPSHOT_NODES = 100000
const DEFAULTS = Object.freeze({
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
})
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key)
const isMap = (value) => value !== null && typeof value === "object" && !Array.isArray(value)

function plainMap(value, name) {
  if (!isMap(value) || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    throw new TypeError(`${name} must be a plain object`)
  }
  return value
}

function freezeJson(value) {
  if (value && typeof value === "object") {
    for (const key of Object.keys(value)) freezeJson(value[key])
    Object.freeze(value)
  }
  return value
}

function string(value, name, nullable = false) {
  if (nullable && value === null) return
  if (typeof value !== "string" || (!nullable && value.trim().length === 0)) {
    throw new TypeError(`${name} must be ${nullable ? "a string or null" : "a nonempty string"}`)
  }
}

export function headerSafeKey(value, name = "apiKey", maxLength = 4096) {
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength ||
      !/^[\x21-\x7e]+$/.test(value)) {
    throw new TypeError(`${name} must be a bounded, nonempty header-safe string`)
  }
  return value
}

function collectorEndpoint(value, allowHttp) {
  string(value, "collectorUrl")
  const raw = value.trim()
  if (/[\s\\?#]/.test(raw)) throw new TypeError("collectorUrl must not contain query, fragment or whitespace")
  let url
  try { url = new URL(raw) } catch { throw new TypeError("collectorUrl must be an absolute URL") }
  if (!/^https?:\/\//i.test(raw) || !url.hostname || url.username || url.password ||
      raw.slice(raw.indexOf("//") + 2).split("/")[0].includes("@") ||
      (url.protocol !== "https:" && !(allowHttp && url.protocol === "http:"))) {
    throw new TypeError("collectorUrl must use HTTPS without credentials (HTTP requires the development option)")
  }
  const path = url.pathname.replace(/\/+$/, "")
  url.pathname = /\/v1\/events:batch$/i.test(path)
    ? path.replace(/\/v1\/events:batch$/i, "/v1/events:batch")
    : `${path}/v1/events:batch`
  return url.href
}

function positiveInteger(value, name, maximum = Number.MAX_SAFE_INTEGER, minimum = 1) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new RangeError(`${name} must be an integer between ${minimum} and ${maximum}`)
  }
}

function optionalCallback(value, name) {
  if (value === undefined || value === null) return undefined
  if (typeof value !== "function") throw new TypeError(`${name} must be a function`)
  return value
}

// Runtime references deliberately remain references; they must not be cloned or frozen.
export function normalizeConfig(input) {
  plainMap(input, "config")
  for (const descriptor of Object.values(Object.getOwnPropertyDescriptors(input))) {
    if (descriptor.get || descriptor.set) throw new TypeError("config must not contain accessors")
  }
  const runtime = input.runtime === undefined ? undefined : plainMap(input.runtime, "runtime")
  const seam = (name, runtimeName = name, fallback = globalThis[name]) =>
    input[name] !== undefined ? input[name] : runtime?.[runtimeName] !== undefined ? runtime[runtimeName] : fallback
  const cfg = {
    appId: headerSafeKey(input.appId, "appId", 256),
    environment: headerSafeKey(input.environment, "environment", 128),
    apiKey: headerSafeKey(input.apiKey),
    allowInsecureHttpForDevelopment: input.allowInsecureHttpForDevelopment ?? false,
    product: input.product,
    surface: input.surface ?? "browser_game",
    sourceType: input.sourceType ?? "game_client",
    schemaVersion: input.schemaVersion ?? 1,
    autoFlush: input.autoFlush ?? true,
    clock: input.clock ?? (() => Date.now()),
    random: input.random ?? (() => Math.random()),
    runtime,
    window: seam("window"),
    document: seam("document"),
    indexedDB: seam("indexedDB"),
    locks: seam("locks", "locks", globalThis.navigator?.locks),
    crypto: seam("crypto"),
    BroadcastChannel: seam("BroadcastChannel"),
    fetchImpl: seam("fetchImpl", "fetch", globalThis.fetch),
    topLevel: input.topLevel,
    secureContext: input.secureContext ?? runtime?.isSecureContext ?? input.window?.isSecureContext ?? runtime?.window?.isSecureContext ?? globalThis.isSecureContext,
    onPermissionRequired: optionalCallback(input.onPermissionRequired, "onPermissionRequired"),
    sessionStartFactory: optionalCallback(input.sessionStartFactory, "sessionStartFactory"),
    eventContextMapper: optionalCallback(input.eventContextMapper, "eventContextMapper"),
    revocationStore: input.revocationStore,
  }
  for (const key of ["allowInsecureHttpForDevelopment", "autoFlush"]) {
    if (typeof cfg[key] !== "boolean") throw new TypeError(`${key} must be a boolean`)
  }
  for (const key of ["topLevel", "secureContext"]) {
    if (cfg[key] !== undefined && typeof cfg[key] !== "boolean") throw new TypeError(`${key} must be a boolean`)
  }
  for (const key of ["clock", "random"]) {
    if (typeof cfg[key] !== "function") throw new TypeError(`${key} must be a function`)
  }
  if (cfg.fetchImpl != null && typeof cfg.fetchImpl !== "function") throw new TypeError("fetchImpl must be a function")
  if (!isMap(cfg.revocationStore)) {
    throw new TypeError("revocationStore must implement readPending, markPending, and clearPending")
  }
  for (const key of ["readPending", "markPending", "clearPending"]) {
    let prototype = cfg.revocationStore
    let descriptor
    while (prototype && !descriptor) {
      descriptor = Object.getOwnPropertyDescriptor(prototype, key)
      prototype = Object.getPrototypeOf(prototype)
    }
    if (!descriptor || descriptor.get || descriptor.set || typeof descriptor.value !== "function") {
      throw new TypeError(`revocationStore.${key} must be a data function property`)
    }
  }
  cfg.collectorUrl = collectorEndpoint(input.collectorUrl, cfg.allowInsecureHttpForDevelopment)
  if (input.previousCollectorUrl !== undefined) {
    cfg.previousCollectorUrl = collectorEndpoint(input.previousCollectorUrl, cfg.allowInsecureHttpForDevelopment)
  }
  for (const [key, fallback] of Object.entries(DEFAULTS)) {
    const value = input[key] === undefined ? fallback : input[key]
    if (key === "retentionDays") {
      if (!Number.isFinite(value) || value <= 0 || value * 86400000 > Number.MAX_SAFE_INTEGER) {
        throw new RangeError("retentionDays must be a finite positive duration")
      }
    } else {
      const maximum = key === "maxEventsPerRequest" || key === "maxTrackBatchEvents" ? 500
        : key === "maxRequestBytes" ? 1048576
        : key === "maxSnapshotDepth" ? 128
        : key === "maxRetries" ? 10
        : ["maxQueueBytes", "maxPendingTrackBytes"].includes(key) ? 104857600
        : key === "maxPendingTrackOperations" ? 1024
        : key.endsWith("Ms") ? 2147483647 : Number.MAX_SAFE_INTEGER
      positiveInteger(value, key, maximum, key === "maxRetries" ? 0 : 1)
    }
    cfg[key] = value
  }
  if (cfg.product !== undefined) string(cfg.product, "product")
  string(cfg.surface, "surface")
  string(cfg.sourceType, "sourceType")
  if (typeof cfg.schemaVersion === "string") string(cfg.schemaVersion, "schemaVersion")
  else positiveInteger(cfg.schemaVersion, "schemaVersion")
  const device = input.defaultDevice === undefined ? {} : detachJson(input.defaultDevice, cfg.maxSnapshotDepth)
  plainMap(device, "defaultDevice")
  validateContextStrings(device, DEVICE_STRINGS, "defaultDevice")
  if (device.platform != null && device.platform !== "web") throw new TypeError("defaultDevice.platform must be web")
  cfg.defaultDevice = freezeJson({ ...device, platform: "web" })
  if (input.legacyIdentityImport !== undefined) {
    const identity = detachJson(input.legacyIdentityImport, cfg.maxSnapshotDepth)
    plainMap(identity, "legacyIdentityImport")
    for (const key of ["pseudoId", "appId", "environment", "policyVersion"]) string(identity[key], `legacyIdentityImport.${key}`)
    if (identity.pseudoId.length > 256 || !/^[a-zA-Z0-9_.-]{1,64}$/.test(identity.policyVersion)) throw new TypeError("legacyIdentityImport must use bounded identity and policy values")
    if (identity.appId !== cfg.appId || identity.environment !== cfg.environment) {
      throw new TypeError("legacyIdentityImport must match appId and environment")
    }
    cfg.legacyIdentityImport = freezeJson({ pseudoId: identity.pseudoId, appId: identity.appId,
      environment: identity.environment, policyVersion: identity.policyVersion })
  }
  return Object.freeze(cfg)
}

export function uuid(cryptoImpl = globalThis.crypto) {
  if (typeof cryptoImpl?.randomUUID === "function") return cryptoImpl.randomUUID()
  if (typeof cryptoImpl?.getRandomValues !== "function") throw new Error("Secure random IDs are unavailable")
  const bytes = new Uint8Array(16)
  cryptoImpl.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function byteLength(value) {
  if (typeof value !== "string") throw new TypeError("byteLength requires a string")
  return new TextEncoder().encode(value).byteLength
}

export function detachJson(value, maxDepth = 32, maxBytes = DEFAULTS.maxPendingTrackBytes) {
  positiveInteger(maxDepth, "maxDepth", 129, 0)
  positiveInteger(maxBytes, "maxBytes", 104857600)
  const ancestors = new Set()
  let nodes = 0
  let characters = 0
  const charge = amount => {
    characters += amount
    // A cheap lower bound prevents building/stringifying an unbounded clone.
    // Exact serialized UTF-8 accounting remains the admission authority.
    if (characters > maxBytes) throw new RangeError("JSON snapshot size exceeded")
  }
  function visit(current, depth) {
    if (++nodes > MAX_SNAPSHOT_NODES || depth > maxDepth) throw new RangeError("JSON snapshot complexity exceeded")
    if (typeof current === "string") { charge(current.length + 2); return current }
    if (current === null || typeof current === "boolean") { charge(4); return current }
    if (typeof current === "number" && Number.isFinite(current)) { charge(String(current).length); return current }
    if (typeof current !== "object" || current === null) throw new TypeError("Only finite JSON values are supported")
    const array = Array.isArray(current)
    if (array ? Object.getPrototypeOf(current) !== Array.prototype :
      ![Object.prototype, null].includes(Object.getPrototypeOf(current))) {
      throw new TypeError("Only plain JSON objects and arrays are supported")
    }
    if (ancestors.has(current)) throw new TypeError("JSON snapshot must not contain cycles")
    ancestors.add(current)
    charge(2)
    const keys = Reflect.ownKeys(current)
    if (keys.length > MAX_SNAPSHOT_NODES + 1 || (array && current.length > MAX_SNAPSHOT_NODES)) throw new RangeError("JSON snapshot complexity exceeded")
    const descriptors = Object.getOwnPropertyDescriptors(current)
    for (const key of keys) {
      const descriptor = descriptors[key]
      if (typeof key !== "string" || descriptor.get || descriptor.set ||
          (!descriptor.enumerable && !(array && key === "length"))) {
        throw new TypeError("JSON snapshot must not contain symbols, accessors or hidden properties")
      }
    }
    const result = array ? [] : {}
    if (array) {
      if (current.length > MAX_SNAPSHOT_NODES || keys.length !== current.length + 1) {
        throw new TypeError("JSON arrays must be dense and have no extra properties")
      }
      for (let index = 0; index < current.length; index += 1) {
        if (!own(descriptors, String(index))) throw new TypeError("JSON arrays must be dense")
        result.push(visit(descriptors[index].value, depth + 1))
      }
    } else {
      for (const key of keys) {
        charge(key.length + 4)
        Object.defineProperty(result, key, { value: visit(descriptors[key].value, depth + 1),
          enumerable: true, writable: true, configurable: true })
      }
    }
    ancestors.delete(current)
    return result
  }
  return visit(value, 0)
}

const USER_STRINGS = ["user_id", "userId", "user_pseudo_id", "userPseudoId", "session_id", "sessionId"]
const DEVICE_STRINGS = ["platform", "app_version", "appVersion", "os_version", "osVersion", "device_model", "deviceModel", "locale", "timezone"]
const TRAFFIC_STRINGS = ["source", "medium", "campaign"]
const GEO_STRINGS = ["country", "country_code", "countryCode", "city", "region", "timezone"]
const COLLECTOR_OWNED = ["appId", "app_id", "environment", "received_at", "receivedAt", "ingest", "ingestContext", "ingest_context",
  "request_id", "requestId", "ip", "ip_hash", "ipHash", "user_agent", "userAgent"]

function validateContextStrings(value, keys, name) {
  for (const key of keys) if (own(value, key)) string(value[key], `${name}.${key}`, true)
}

function timestamp(value) {
  string(value, "eventTimestamp")
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/.exec(value)
  if (!match || !Number.isFinite(Date.parse(value))) throw new TypeError("eventTimestamp must be an RFC3339 timestamp")
  const [, year, month, day, hour, minute, second, , offsetHour, offsetMinute] = match
  const leap = Number(year) % 4 === 0 && (Number(year) % 100 !== 0 || Number(year) % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  if (+month < 1 || +month > 12 || +day < 1 || +day > days[+month - 1] || +hour > 23 ||
      +minute > 59 || +second > 59 || +(offsetHour ?? 0) > 23 || +(offsetMinute ?? 0) > 59) {
    throw new TypeError("eventTimestamp must be a valid calendar timestamp")
  }
}

function validateInput(input) {
  plainMap(input, "event")
  for (const key of COLLECTOR_OWNED) if (own(input, key)) throw new TypeError(`${key} is SDK/Collector-owned`)
  for (const key of ["eventName", "event_name"]) if (own(input, key)) string(input[key], key)
  string(input.eventName ?? input.event_name, "eventName")
  for (const key of ["eventId", "event_id"]) if (own(input, key) && input[key] !== null) string(input[key], key)
  for (const key of ["eventTimestamp", "event_timestamp"]) if (own(input, key) && input[key] !== null) timestamp(input[key])
  const maps = ["user", "device", "eventParams", "event_params", "userProperties", "user_properties",
    "trafficSource", "traffic_source", "geo", "consent"]
  for (const key of maps) if (own(input, key) && input[key] !== null) plainMap(input[key], key)
  if (input.user) validateContextStrings(input.user, USER_STRINGS, "user")
  if (input.device) {
    validateContextStrings(input.device, DEVICE_STRINGS, "device")
    if (input.device.platform != null && input.device.platform !== "web") throw new TypeError("device.platform must be web")
  }
  for (const key of ["trafficSource", "traffic_source"]) if (input[key]) validateContextStrings(input[key], TRAFFIC_STRINGS, key)
  if (input.geo) validateContextStrings(input.geo, GEO_STRINGS, "geo")
  if (input.consent) {
    for (const key of ["analytics_storage", "analyticsStorage", "ads_storage", "adsStorage", "personalization"]) {
      if (own(input.consent, key) && input.consent[key] !== null && typeof input.consent[key] !== "boolean") {
        throw new TypeError(`consent.${key} must be boolean or null`)
      }
    }
    for (const key of ["analytics_storage", "analyticsStorage"]) {
      if (own(input.consent, key) && input.consent[key] !== true) throw new TypeError("analytics_storage must be true when supplied")
    }
  }
}

export function snapshotInputs(inputs, cfg, now = cfg.clock()) {
  if (!Array.isArray(inputs) || inputs.length === 0 || inputs.length > cfg.maxTrackBatchEvents) {
    throw new RangeError("inputs must be a nonempty, bounded event array")
  }
  if (!Number.isFinite(now)) throw new TypeError("clock must return a finite timestamp")
  const capturedTime = new Date(now).toISOString()
  // The batch container does not consume an event's depth allowance.
  const container = detachJson(inputs, cfg.maxSnapshotDepth + 1, cfg.maxPendingTrackBytes)
  const snapshots = container.map((input) => {
    validateInput(input)
    input.eventName = input.eventName ?? input.event_name
    input.eventId = input.eventId ?? input.event_id ?? uuid(cfg.crypto)
    input.eventTimestamp = input.eventTimestamp ?? input.event_timestamp ?? capturedTime
    detachJson(input, cfg.maxSnapshotDepth)
    return input
  })
  const bytes = byteLength(JSON.stringify(snapshots))
  if (bytes > cfg.maxPendingTrackBytes) throw new RangeError("Detached inputs exceed maxPendingTrackBytes")
  return Object.freeze({ inputs: freezeJson(snapshots), bytes })
}

export async function buildEnvelope(input, cfg, { installationId, sessionId }) {
  string(installationId, "installationId")
  string(sessionId, "sessionId")
  // Re-snapshot before the shared normalizer's first await, including hook outputs.
  let detached = detachJson(input, cfg.maxSnapshotDepth)
  validateInput(detached)
  const eventId = detached.eventId ?? detached.event_id
  const eventTimestamp = detached.eventTimestamp ?? detached.event_timestamp
  if (eventId == null || eventTimestamp == null) {
    detached = snapshotInputs([detached], cfg).inputs[0]
  } else {
    detached.eventName = detached.eventName ?? detached.event_name
    detached.eventId = eventId
    detached.eventTimestamp = eventTimestamp
    freezeJson(detached)
  }
  for (const key of ["session_id", "sessionId"]) {
    if (detached.user && own(detached.user, key) && detached.user[key] != null && detached.user[key] !== sessionId) {
      throw new TypeError("user.session_id is SDK-owned")
    }
  }
  const eventInput = { ...detached,
    user: { ...detached.user, session_id: sessionId },
    device: { ...detached.device, platform: "web" },
    consent: { ...detached.consent, analytics_storage: true },
  }
  const envelope = await buildCollectorEvent({ eventInput, config: cfg, identityAdapter: {
    getUserPseudoId: () => installationId,
    getSessionId: () => sessionId,
  } })
  // Preserve arbitrary detached JSON context, not only the legacy normalizer's known keys.
  envelope.user = { ...eventInput.user, ...envelope.user }
  envelope.device = { ...cfg.defaultDevice, ...eventInput.device, ...envelope.device, platform: "web" }
  envelope.user.session_id = sessionId
  envelope.consent.analytics_storage = true
  const result = detachJson(envelope, cfg.maxSnapshotDepth)
  if (!validateFrozen(JSON.stringify(result), cfg)) throw new TypeError("Invalid or oversized Collector envelope")
  return freezeJson(result)
}

export function serializeBatch(records) {
  if (!Array.isArray(records) || records.some((record) => typeof record?.payloadJson !== "string")) {
    throw new TypeError("records must contain frozen payloadJson strings")
  }
  return `{"events":[${records.map((record) => record.payloadJson).join(",")}]}`
}

// Recovery policy/selection is call-time authority, not mutable host state.
export function snapshotRecoveryOptions(options) {
  const value = detachJson(options, 4, 4096)
  plainMap(value, "recovery options")
  if (!Number.isSafeInteger(value.expectedFaultRevision) || value.expectedFaultRevision < 1 ||
      !Number.isSafeInteger(value.expectedQueueRevision) || value.expectedQueueRevision < 0 ||
      typeof value.reasonCode !== "string" || value.reasonCode.length > 64 ||
      typeof value.policyVersion !== "string" || !/^[a-zA-Z0-9_.-]{1,64}$/.test(value.policyVersion)) throw new TypeError("Invalid recovery policy")
  return Object.freeze({ expectedFaultRevision: value.expectedFaultRevision, expectedQueueRevision: value.expectedQueueRevision,
    reasonCode: value.reasonCode, policyVersion: value.policyVersion })
}

export function validateFrozen(payloadJson, cfg) {
  try {
    if (typeof payloadJson !== "string" || byteLength(serializeBatch([{ payloadJson }])) > cfg.maxRequestBytes) return false
    const event = detachJson(JSON.parse(payloadJson), cfg.maxSnapshotDepth)
    plainMap(event, "event")
    for (const key of ["event_id", "event_name"]) string(event[key], key)
    timestamp(event.event_timestamp)
    if (event.app_id !== cfg.appId || event.environment !== cfg.environment) return false
    if (COLLECTOR_OWNED.filter((key) => !["app_id", "environment"].includes(key)).some((key) => own(event, key))) return false
    for (const key of ["user", "device", "event_params", "user_properties", "traffic_source", "geo", "consent"]) plainMap(event[key], key)
    validateContextStrings(event.user, USER_STRINGS, "user")
    validateContextStrings(event.device, DEVICE_STRINGS, "device")
    validateContextStrings(event.traffic_source, TRAFFIC_STRINGS, "traffic_source")
    validateContextStrings(event.geo, GEO_STRINGS, "geo")
    string(event.user.user_pseudo_id, "user_pseudo_id")
    string(event.user.session_id, "session_id")
    if (own(event.user, "sessionId") && event.user.sessionId !== null && event.user.sessionId !== event.user.session_id) return false
    if (event.device.platform !== "web" || event.consent.analytics_storage !== true) return false
    if (own(event.consent, "analyticsStorage") && event.consent.analyticsStorage !== true) return false
    for (const key of ["ads_storage", "adsStorage", "personalization"]) {
      if (own(event.consent, key) && event.consent[key] !== null && typeof event.consent[key] !== "boolean") return false
    }
    return true
  } catch { return false }
}

// Called on a transaction candidate. It contains no event, identity or request data.
export function addReceipt(state, operation, reasonCode, policyVersion, count, now, cryptoImpl) {
  for (const [name, value] of Object.entries({ operation, reasonCode, policyVersion })) string(value, name)
  positiveInteger(count, "count", Number.MAX_SAFE_INTEGER, 0)
  if (!Number.isFinite(now)) throw new TypeError("receipt timestamp must be finite")
  const faultRevision = state.meta?.faultRevision ?? state.meta?.fault?.revision ?? state.faultRevision ?? state.fault?.revision ?? 0
  positiveInteger(faultRevision, "faultRevision", Number.MAX_SAFE_INTEGER, 0)
  const order = (state.meta?.receiptSequence ?? 0) + 1
  positiveInteger(order, "receiptSequence", Number.MAX_SAFE_INTEGER)
  if (state.meta) state.meta.receiptSequence = order
  const receipt = Object.freeze({ operationId: uuid(cryptoImpl), operation, reasonCode, policyVersion,
    count, createdAt: now, faultRevision, order })
  state.receipts = [...(state.receipts ?? []), receipt].slice(-64)
  return receipt
}
