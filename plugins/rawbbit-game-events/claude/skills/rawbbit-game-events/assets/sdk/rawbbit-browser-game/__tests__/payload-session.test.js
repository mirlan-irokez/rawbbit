import test from "node:test"
import assert from "node:assert/strict"
import { webcrypto } from "node:crypto"

import { addReceipt, buildEnvelope, byteLength, detachJson, headerSafeKey, normalizeConfig,
  serializeBatch, snapshotInputs, uuid, validateFrozen } from "../payload.js"
import { emptySession, eventContext, prepareSession, sessionCandidate } from "../session.js"

const NOW = Date.parse("2026-10-13T12:00:00.000Z")
const TIME = new Date(NOW).toISOString()
const ID = "12345678-1234-4234-8234-123456789abc"
const secure = { randomUUID: () => ID }
const revocationStore = { readPending: async () => null, markPending: async () => ID, clearPending: async () => true }
const config = (overrides = {}) => normalizeConfig({ collectorUrl: "https://collector.example",
  appId: "com.example.game", apiKey: "synthetic-ingest-key", environment: "test",
  clock: () => NOW, crypto: secure, revocationStore, ...overrides })
const identity = { installationId: "installation", sessionId: "session" }
const envelope = (overrides = {}, cfg = config()) => buildEnvelope({ eventName: "action", ...overrides }, cfg, identity)

test("configuration has explicit scope, canonical endpoint and frozen defaults", () => {
  const cfg = config()
  assert.equal(cfg.collectorUrl, "https://collector.example/v1/events:batch")
  assert.equal(cfg.environment, "test")
  assert.equal(cfg.surface, "browser_game")
  assert.equal(cfg.sourceType, "game_client")
  assert.equal(cfg.schemaVersion, 1)
  assert.equal(cfg.autoFlush, true)
  assert.deepEqual(cfg.defaultDevice, { platform: "web" })
  for (const [key, value] of Object.entries({ maxQueueBytes: 10485760, retentionDays: 7,
    sessionTimeoutMs: 1800000, maxEventsPerRequest: 500, maxRequestBytes: 1048576,
    timeoutMs: 10000, maxRetries: 2, retryCooldownMs: 60000, backoffMs: 250,
    maxPendingTrackOperations: 64, maxPendingTrackBytes: 2097152, maxTrackBatchEvents: 500,
    lockTimeoutMs: 3000, storageTimeoutMs: 3000, flushTimeoutMs: 15000,
    maxResponseBytes: 65536, maxSnapshotDepth: 32 })) assert.equal(cfg[key], value, key)
  assert.ok(Object.isFrozen(cfg))
  assert.ok(Object.isFrozen(cfg.defaultDevice))
  assert.equal(config({ collectorUrl: "https://COLLECTOR.example:443/base///" }).collectorUrl,
    "https://collector.example/base/v1/events:batch")
  assert.equal(config({ collectorUrl: "https://collector.example/v1/events:batch/" }).collectorUrl,
    cfg.collectorUrl)
  assert.equal(config({ previousCollectorUrl: "https://previous.example/" }).previousCollectorUrl,
    "https://previous.example/v1/events:batch")
})

test("configuration requires a typed host-persisted revocation store", () => {
  assert.throws(() => normalizeConfig({ collectorUrl: "https://collector.example", appId: "game", apiKey: "key", environment: "test" }), /revocationStore/)
  for (const revocationStore of [null, {}, { readPending() {}, markPending() {} },
    { get readPending() { throw new Error("getter must not execute") }, markPending() {}, clearPending() {} }]) {
    assert.throws(() => config({ revocationStore }), /revocationStore/)
  }
  const store = { readPending: async () => null, markPending: async () => ID, clearPending: async () => true }
  assert.strictEqual(config({ revocationStore: store }).revocationStore, store)
  class HostJournal {
    readPending() { return null }
    markPending() { return ID }
    clearPending() { return true }
  }
  assert.ok(config({ revocationStore: new HostJournal() }))
})

test("runtime seams preserve references without touching localStorage", () => {
  const window = { isSecureContext: true }
  Object.defineProperty(window, "localStorage", { get() { throw new Error("must not read storage") } })
  const runtime = { window, document: {}, indexedDB: {}, locks: {}, crypto: webcrypto,
    BroadcastChannel: class Channel {}, isSecureContext: true, fetch: async () => {} }
  const clock = () => 100
  const random = () => 0.25
  const callback = () => {}
  const cfg = config({ crypto: undefined, runtime, clock, random,
    onPermissionRequired: callback, sessionStartFactory: callback, eventContextMapper: callback })
  assert.strictEqual(cfg.runtime, runtime)
  for (const key of ["window", "document", "indexedDB", "locks", "crypto", "BroadcastChannel"]) {
    assert.strictEqual(cfg[key], runtime[key])
    assert.equal(Object.isFrozen(runtime[key]), false)
  }
  assert.strictEqual(cfg.fetchImpl, runtime.fetch)
  assert.equal(cfg.secureContext, true)
  assert.strictEqual(cfg.clock, clock)
  assert.strictEqual(cfg.random, random)
  assert.strictEqual(cfg.onPermissionRequired, callback)
  const injectedFetch = () => {}
  assert.strictEqual(config({ runtime, fetchImpl: injectedFetch }).fetchImpl, injectedFetch)
  assert.equal(config({ autoFlush: false, secureContext: false, topLevel: false }).autoFlush, false)
})

test("unsafe config and endpoint options are rejected", () => {
  for (const collectorUrl of ["", "//collector.example", "ftp://collector.example", "javascript:alert(1)",
    "https://user:pass@collector.example", "https://@collector.example", "https://collector.example?",
    "https://collector.example#", "https://collector.example/?secret=x", "https://collector.example/#x",
    "https://collector.example/space here", "https:\\collector.example", "http://localhost:8080"]) {
    assert.throws(() => config({ collectorUrl }), undefined, collectorUrl)
  }
  assert.equal(config({ collectorUrl: "http://localhost:8080", allowInsecureHttpForDevelopment: true }).collectorUrl,
    "http://localhost:8080/v1/events:batch")
  assert.throws(() => config({ collectorUrl: "ftp://localhost", allowInsecureHttpForDevelopment: true }))
  for (const [key, value] of [["environment", undefined], ["environment", "prod\r\nInjected:x"],
    ["appId", ""], ["apiKey", "a\nb"], ["apiKey", "é"], ["apiKey", "x".repeat(4097)],
    ["maxQueueBytes", 0], ["maxQueueBytes", null], ["timeoutMs", Infinity], ["lockTimeoutMs", 2147483648],
    ["maxRetries", -1], ["maxPendingTrackOperations", 1.5], ["maxEventsPerRequest", 501],
    ["maxRequestBytes", 1048577], ["maxTrackBatchEvents", 501], ["maxSnapshotDepth", 129],
    ["retentionDays", NaN], ["autoFlush", "yes"], ["allowInsecureHttpForDevelopment", 1],
    ["defaultDevice", []], ["defaultDevice", { platform: "android" }], ["clock", 1],
    ["sessionStartFactory", {}], ["eventContextMapper", "mapper"], ["onPermissionRequired", true]]) {
    assert.throws(() => config({ [key]: value }), undefined, key)
  }
  assert.equal(config({ maxRetries: 0 }).maxRetries, 0)
  assert.equal(headerSafeKey("visible-ascii"), "visible-ascii")
})

test("defaults and optional reviewed legacy identity are detached", () => {
  const defaultDevice = { locale: "en", properties: { capability: ["input"] } }
  const legacyIdentityImport = { pseudoId: "reviewed-id", appId: "com.example.game",
    environment: "test", policyVersion: "review-1" }
  const cfg = config({ defaultDevice, legacyIdentityImport })
  defaultDevice.properties.capability[0] = "changed"
  legacyIdentityImport.pseudoId = "changed"
  assert.equal(cfg.defaultDevice.properties.capability[0], "input")
  assert.equal(cfg.legacyIdentityImport.pseudoId, "reviewed-id")
  assert.ok(Object.isFrozen(cfg.defaultDevice.properties.capability))
  for (const change of [{ environment: "prod" }, { appId: "another" }, { policyVersion: 1 }, { pseudoId: "" }]) {
    assert.throws(() => config({ legacyIdentityImport: { ...legacyIdentityImport, ...change } }))
  }
})

test("detachJson retains finite JSON and safely preserves __proto__ keys", () => {
  const original = JSON.parse('{"__proto__":{"polluted":true},"items":[null,true,1.5,"🎮"],"nested":{"x":1}}')
  const result = detachJson(original)
  original.nested.x = 2
  assert.deepEqual(result.items, [null, true, 1.5, "🎮"])
  assert.equal(result.nested.x, 1)
  assert.equal(Object.getPrototypeOf(result), Object.prototype)
  assert.equal(Object.hasOwn(result, "__proto__"), true)
  assert.equal(result.__proto__.polluted, true)
  assert.equal({}.polluted, undefined)
  const shared = { x: 1 }
  assert.deepEqual(detachJson({ a: shared, b: shared }), { a: { x: 1 }, b: { x: 1 } })
  assert.notStrictEqual(detachJson({ a: shared }).a, shared)
  assert.deepEqual(detachJson(Object.assign(Object.create(null), { x: null })), { x: null })
})

test("detachJson rejects unsupported values, cycles, classes, sparse arrays and getters without calling them", () => {
  const cyclic = {}; cyclic.self = cyclic
  const getter = Object.defineProperty({}, "value", { enumerable: true, get() { throw new Error("getter was called") } })
  const hiddenGetter = Object.defineProperty({}, "hidden", { get() { throw new Error("getter was called") } })
  const extra = []; extra.metadata = true
  const symbols = { [Symbol("secret")]: 1 }
  class Context { constructor() { this.x = 1 } }
  for (const value of [undefined, NaN, Infinity, -Infinity, 1n, Symbol(), () => {}, new Date(),
    new Context(), new Uint8Array(2), new Map(), cyclic, getter, hiddenGetter, extra, symbols,
    new Array(1), { x: undefined }, { x: () => {} }]) {
    assert.throws(() => detachJson(value), undefined, String(typeof value))
  }
  assert.deepEqual(detachJson({ x: {} }, 1), { x: {} })
  assert.throws(() => detachJson({ x: { y: 1 } }, 1), RangeError)
  assert.throws(() => detachJson(Array.from({ length: 100001 }, () => null)), /dense|complexity/)
  assert.throws(() => detachJson({ x: Array.from({ length: 99999 }, () => null) }), /complexity/)
})

test("UUIDs are secure and UTF-8 accounting matches complete batch bytes", () => {
  assert.equal(uuid(secure), ID)
  assert.equal(uuid({ getRandomValues(bytes) { bytes.fill(255); return bytes } }),
    "ffffffff-ffff-4fff-bfff-ffffffffffff")
  assert.throws(() => uuid({}), /Secure/)
  assert.match(uuid(webcrypto), /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  for (const value of ["", "ascii", "é", "🎮", "\ud800", "line\n\"quoted\""]) {
    assert.equal(byteLength(value), Buffer.byteLength(value, "utf8"))
  }
  assert.throws(() => byteLength(1))
  const a = ' {"number":1e0,"escaped":"\\u0061"} '
  const b = '{"emoji":"🎮"}'
  assert.equal(serializeBatch([{ payloadJson: a }, { payloadJson: b }]), `{"events":[${a},${b}]}`)
  assert.equal(serializeBatch([]), '{"events":[]}')
  assert.throws(() => serializeBatch([{ payload: {} }]))
})

test("snapshots synchronously detach aliases and assign event identity/time once", async () => {
  let ids = 0
  const cfg = config({ crypto: { randomUUID() { ids += 1; return ID } } })
  const input = { event_name: "move", event_id: null, event_timestamp: null,
    event_params: { moves: [{ x: 1 }], nullable: null }, user_properties: null,
    user: { userPseudoId: "approved-override" }, traffic_source: null, geo: null, consent: null }
  const snapshot = snapshotInputs([input], cfg, NOW)
  assert.equal(ids, 1)
  assert.equal(snapshot.inputs[0].eventId, ID)
  assert.equal(snapshot.inputs[0].eventTimestamp, TIME)
  assert.ok(Object.isFrozen(snapshot.inputs[0].event_params.moves[0]))
  assert.equal(snapshot.bytes, byteLength(JSON.stringify(snapshot.inputs)))
  input.event_params.moves[0].x = 9
  const promise = buildEnvelope(snapshot.inputs[0], cfg, identity)
  input.user.userPseudoId = "changed"
  const event = await promise
  assert.equal(ids, 1)
  assert.equal(event.event_id, ID)
  assert.equal(event.event_timestamp, TIME)
  assert.equal(event.event_params.moves[0].x, 1)
  assert.equal(event.user.user_pseudo_id, "approved-override")
  assert.deepEqual(event.user_properties, {})
  assert.deepEqual(event.traffic_source, {})
  assert.ok(Object.isFrozen(event))
})

test("snapshot validation covers both aliases, context types, invalid dates and all batch bounds", () => {
  const cfg = config()
  for (const input of [{}, { eventName: "" }, { eventName: 1 }, { eventName: "ok", event_name: 1 },
    { eventName: "ok", event_id: false }, { eventName: "ok", event_timestamp: 1 },
    { eventName: "ok", eventTimestamp: "2026-02-31T00:00:00Z" },
    { eventName: "ok", eventTimestamp: "2026-01-01" },
    { eventName: "ok", user: { userId: 1 } }, { eventName: "ok", device: { locale: 1 } },
    { eventName: "ok", trafficSource: { campaign: [] } }, { eventName: "ok", geo: { country: 3 } },
    { eventName: "ok", eventParams: [] }, { eventName: "ok", userProperties: [] },
    { eventName: "ok", consent: { analytics_storage: false } },
    { eventName: "ok", consent: { analyticsStorage: false } },
    { eventName: "ok", consent: { personalization: "yes" } },
    { eventName: "ok", eventParams: { invalid: Infinity } }]) {
    assert.throws(() => snapshotInputs([input], cfg), undefined, JSON.stringify(input))
  }
  for (const key of ["appId", "app_id", "environment", "received_at", "receivedAt", "ingest", "ingest_context", "ip_hash", "request_id"]) {
    assert.throws(() => snapshotInputs([{ eventName: "ok", [key]: null }], cfg), /owned/)
  }
  assert.throws(() => snapshotInputs([], cfg))
  assert.throws(() => snapshotInputs({}, cfg))
  assert.equal(snapshotInputs(Array.from({ length: 500 }, () => ({ eventName: "ok" })), cfg).inputs.length, 500)
  assert.throws(() => snapshotInputs(Array.from({ length: 501 }, () => ({ eventName: "ok" })), cfg))
  assert.throws(() => snapshotInputs([{ eventName: "ok" }], config({ maxPendingTrackBytes: 1 })), /maxPendingTrackBytes|snapshot size exceeded/)
  assert.throws(() => snapshotInputs([{ eventName: "ok", eventParams: { deep: { x: 1 } } }], config({ maxSnapshotDepth: 2 })))
  assert.throws(() => snapshotInputs([{ eventName: "ok" }], cfg, Infinity))
  const events = snapshotInputs([{ eventName: "ok", eventId: "caller-id", eventTimestamp: "2026-10-13T12:00:00+00:00" }], cfg)
  assert.equal(events.inputs[0].eventId, "caller-id")
})

test("envelope construction detaches direct input before awaiting and reuses captured clocks", async () => {
  let clocks = 0
  const cfg = config({ clock() { clocks += 1; return NOW } })
  const input = { eventName: "action", eventId: "captured-id", eventTimestamp: TIME,
    eventParams: { items: [{ value: 1 }] }, user: { userPseudoId: "approved" } }
  const pending = buildEnvelope(input, cfg, identity)
  input.eventParams.items[0].value = 9
  input.user.userPseudoId = "changed"
  const event = await pending
  assert.equal(clocks, 0)
  assert.equal(event.event_params.items[0].value, 1)
  assert.equal(event.user.user_pseudo_id, "approved")
  assert.equal(event.event_timestamp, TIME)
  assert.equal(event.event_id, "captured-id")
})

test("shared normalizer enforces owned context while preserving arbitrary nested JSON and caller pseudo-ID", async () => {
  const cfg = config({ product: "sample", defaultDevice: { locale: "en", custom: { x: [1] } } })
  const event = await envelope({ event_name: "snake", eventId: "caller-event", user: {
    user_id: "account", user_pseudo_id: "approved", custom: { traits: [null, true] }, sessionId: "session" },
    device: { appVersion: "2", custom: { x: [2] } },
    event_params: { surface: "adapter", nullable: null, level: { tiles: [1, 2] } },
    user_properties: { nested: { flags: [true] } }, traffic_source: { source: "host", nested: { x: 1 } },
    geo: { country: null, nested: { x: 2 } }, consent: { ads_storage: false } }, cfg)
  assert.equal(event.event_name, "action")
  assert.equal(event.event_id, "caller-event")
  assert.equal(event.app_id, cfg.appId)
  assert.equal(event.environment, cfg.environment)
  assert.equal(event.user.user_pseudo_id, "approved")
  assert.equal(event.user.session_id, "session")
  assert.deepEqual(event.user.custom, { traits: [null, true] })
  assert.equal(event.device.platform, "web")
  assert.equal(event.device.app_version, "2")
  assert.deepEqual(event.device.custom, { x: [2] })
  assert.equal(event.event_params.product, "sample")
  assert.equal(event.event_params.surface, "adapter")
  assert.equal(event.event_params.source_type, "game_client")
  assert.equal(event.event_params.schema_version, 1)
  assert.deepEqual(event.event_params.level, { tiles: [1, 2] })
  assert.deepEqual(event.traffic_source.nested, { x: 1 })
  assert.deepEqual(event.geo.nested, { x: 2 })
  assert.equal(event.consent.analytics_storage, true)
  assert.equal(event.consent.ads_storage, false)
  assert.ok(validateFrozen(JSON.stringify(event), cfg))
  assert.ok(Object.isFrozen(event.user.custom.traits))
  for (const input of [{ user: { session_id: "other" } }, { user: { sessionId: "other" } },
    { device: { platform: "native" } }, { consent: { analytics_storage: false } }]) {
    await assert.rejects(envelope(input, cfg))
  }
})

test("frozen validation fails closed on tampering, finite JSON, context and full single-request byte limits", async () => {
  const cfg = config()
  const event = await envelope()
  const payloadJson = JSON.stringify(event)
  const bytes = byteLength(serializeBatch([{ payloadJson }]))
  assert.equal(validateFrozen(payloadJson, config({ maxRequestBytes: bytes })), true)
  assert.equal(validateFrozen(payloadJson, config({ maxRequestBytes: bytes - 1 })), false)
  for (const changed of [{ app_id: "other" }, { environment: "prod" }, { event_id: null },
    { event_timestamp: "invalid" }, { user: { ...event.user, session_id: null } },
    { user: { ...event.user, user_pseudo_id: null } }, { device: { platform: "native" } },
    { user: { ...event.user, sessionId: "conflicting" } },
    { consent: {} }, { consent: { analytics_storage: false } },
    { consent: { analytics_storage: true, analyticsStorage: false } }, { received_at: TIME },
    { ingest: {} }, { event_params: null }, { geo: [] }]) {
    assert.equal(validateFrozen(JSON.stringify({ ...event, ...changed }), cfg), false, JSON.stringify(changed))
  }
  for (const invalid of [null, "", "[]", "null", '{"event_params":{"x":NaN}}',
    payloadJson.replace('"event_params":{', '"event_params":{"overflow":1e400,')]) {
    assert.equal(validateFrozen(invalid, cfg), false)
  }
  await assert.rejects(envelope({}, config({ maxRequestBytes: bytes - 1 })), /oversized/)
})

test("receipt retention is bounded, payload-free and captures the fault revision", () => {
  let next = 0
  const crypto = { randomUUID() { return `receipt-${++next}` } }
  const state = { meta: { faultRevision: 7 }, records: [{ payloadJson: "secret" }], receipts: [] }
  for (let count = 0; count < 70; count += 1) addReceipt(state, "discard", "reviewed", "policy-1", count, NOW, crypto)
  assert.equal(state.receipts.length, 64)
  assert.equal(state.receipts[0].operationId, "receipt-7")
  assert.deepEqual(Object.keys(state.receipts[0]).sort(),
    ["operationId", "operation", "reasonCode", "policyVersion", "count", "createdAt", "faultRevision", "order"].sort())
  assert.equal(state.receipts[0].faultRevision, 7)
  state.meta.faultRevision = 8
  assert.equal(state.receipts[0].faultRevision, 7)
  assert.throws(() => addReceipt(state, "discard", "", "policy", 1, NOW, crypto))
  assert.throws(() => addReceipt(state, "discard", "reason", "policy", -1, NOW, crypto))
  assert.equal(state.records[0].payloadJson, "secret")
})

test("shared sessions use a strict inactivity threshold and do not rewind on clock rollback", () => {
  const timeout = 1800000
  const previous = { id: "shared", lastActivity: NOW, counter: 4 }
  let calls = 0
  const create = () => { calls += 1; return "new-session" }
  for (const time of [NOW - 100, NOW, NOW + timeout - 1, NOW + timeout]) {
    const candidate = sessionCandidate(previous, time, timeout, create)
    assert.equal(candidate.id, "shared")
    assert.equal(candidate.counter, 4)
    assert.equal(candidate.isNewSession, false)
    assert.equal(candidate.lastActivity, Math.max(time, NOW))
  }
  assert.equal(calls, 0)
  const expired = prepareSession(previous, NOW + timeout + 1, timeout, create)
  assert.deepEqual(expired, { id: "new-session", isNewSession: true, counter: 0, lastActivity: NOW + timeout + 1 })
  const fresh = sessionCandidate(emptySession(), NOW, timeout, create)
  assert.equal(fresh.isNewSession, true)
  assert.equal(fresh.counter, 0)
  assert.deepEqual(previous, { id: "shared", lastActivity: NOW, counter: 4 })
  assert.deepEqual(emptySession(), { id: null, lastActivity: 0, counter: 0 })
})

test("failed candidate admission consumes no session state; safe counters never wrap", () => {
  const previous = emptySession()
  const first = sessionCandidate(previous, NOW, 1800000, () => "uncommitted")
  const retry = sessionCandidate(previous, NOW, 1800000, () => "retry")
  assert.equal(first.counter, 0)
  assert.equal(retry.counter, 0)
  assert.notEqual(first.id, retry.id)
  assert.deepEqual(previous, emptySession())
  for (const counter of [-1, 0.5, Number.MAX_SAFE_INTEGER, Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => sessionCandidate({ id: "old", lastActivity: NOW, counter }, NOW, 1800000, () => "new"))
  }
  assert.throws(() => eventContext(first, Number.MAX_SAFE_INTEGER + 1, TIME, false))
  assert.throws(() => eventContext(first, 0, TIME, false))
  assert.throws(() => sessionCandidate(previous, NOW, 0, () => "new"))
  assert.throws(() => sessionCandidate(previous, NaN, 1800000, () => "new"))
  assert.throws(() => sessionCandidate(previous, NOW, 1800000, () => null))
})

test("immutable event contexts contain no automatic events or game vocabulary", () => {
  const candidate = sessionCandidate(emptySession(), NOW, 1800000, () => "session")
  assert.deepEqual(eventContext(candidate, 2, TIME, true), { sessionId: "session", isNewSession: true,
    eventSequence: 2, sessionStartedEventSequence: 1, triggeringEventSequence: 2, eventTimestamp: TIME })
  assert.deepEqual(eventContext(candidate, 1, TIME, false), { sessionId: "session", isNewSession: true,
    eventSequence: 1, sessionStartedEventSequence: 0, triggeringEventSequence: 1, eventTimestamp: TIME })
  const context = eventContext(candidate, 3, TIME, true)
  assert.ok(Object.isFrozen(context))
  assert.throws(() => { context.eventSequence = 9 }, TypeError)
})
