import test from "node:test"
import assert from "node:assert/strict"
import { createBrowserGameClient } from "../index.js"
import { makeHarness, FakeWebLocks, bounded, deferred, collectorResponse, grant, stored, readState } from "./helpers.js"

test("fake locks queue exclusively, support ifAvailable and cancel only waiters", async () => {
  const locks = new FakeWebLocks(), release = deferred(), entered = deferred()
  const activeAbort = new AbortController()
  const active = locks.request("scope", { signal: activeAbort.signal }, async () => {
    entered.resolve(); await release.promise; return "first"
  })
  await bounded(entered.promise)
  activeAbort.abort()
  assert.equal(locks.held("scope"), true)
  assert.equal(await locks.request("scope", { ifAvailable: true }, lock => lock), null)
  const abort = new AbortController()
  const waiting = locks.request("scope", { signal: abort.signal }, () => assert.fail("cancelled waiter ran"))
  const rejected = assert.rejects(waiting, { name: "AbortError" })
  abort.abort(); await rejected
  const second = locks.request("scope", {}, () => "second")
  release.resolve()
  assert.equal(await bounded(active), "first")
  assert.equal(await bounded(second), "second")
  assert.equal(locks.held("scope"), false)
})

test("a new controller and denied consent produce no automatic events", async t => {
  const h = makeHarness(t), client = await h.create()
  assert.equal((await client.track({ eventName: "before" })).status, "consentUnknown")
  assert.equal((await client.flush()).status, "paused")
  assert.equal((await client.confirmConsent(() => false)).status, "withdrawn")
  assert.equal((await client.track({ eventName: "denied" })).status, "consentWithdrawn")
  assert.equal((await readState(client)).records.length, 0)
  assert.equal(h.requests.length, 0)
  await grant(client)
  assert.equal((await readState(client)).records.length, 0)
})

test("track synchronously detaches the caller's input before its first await", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client)
  const input = { eventName: "original", eventParams: { nested: [{ score: 1 }] }, userProperties: { rank: "old" } }
  const pending = client.track(input)
  input.eventName = "mutated"; input.eventParams.nested[0].score = 9; input.userProperties.rank = "new"
  h.advance(100)
  const result = await bounded(pending)
  assert.equal(result.status, "durablyStored")
  const event = JSON.parse((await readState(client)).records[0].payloadJson)
  assert.equal(event.event_name, "original")
  assert.equal(event.event_params.nested[0].score, 1)
  assert.equal(event.user_properties.rank, "old")
  assert.equal(event.event_timestamp, new Date(h.now - 100).toISOString())
  assert.equal(event.event_id, result.eventIds[0])
})

test("prefix and mapper contexts use the first trigger and map only requested events", async t => {
  const h = makeHarness(t), factories = [], mappings = []
  const client = await h.create({
    sessionStartFactory(context, input) {
      factories.push({ context, input })
      assert.equal(h.locks.held(`${client.scope}:state`), false)
      assert.equal(h.locks.held(`${client.scope}:sender`), false)
      assert.ok(Object.isFrozen(context)); assert.ok(Object.isFrozen(input.eventParams))
      return { eventName: "host_session", eventParams: { marker: input.eventParams.marker } }
    },
    eventContextMapper(context, input) {
      mappings.push({ context, input })
      assert.equal(h.locks.held(`${client.scope}:state`), false)
      assert.equal(h.locks.held(`${client.scope}:sender`), false)
      return { eventParams: { sequence: context.eventSequence }, userProperties: { tier: "mapped" },
        trafficSource: { source: "host" }, geo: { country: "KG" }, device: { locale: "en" } }
    },
  })
  await grant(client)
  const timestamp = "2026-10-12T10:00:00.000Z"
  const result = await stored(client, [{ eventName: "first", eventTimestamp: timestamp, eventParams: { marker: 7 } },
    { eventName: "second", eventParams: { marker: 8 } }])
  assert.equal(factories.length, 1); assert.equal(mappings.length, 2)
  assert.equal(factories[0].input.eventName, "first")
  assert.equal(factories[0].context.eventSequence, 1)
  assert.deepEqual(mappings.map(call => call.context.eventSequence), [2, 3])
  assert.ok(mappings.every(call => call.context.sessionStartedEventSequence === 1))
  assert.deepEqual(mappings.map(call => call.context.triggeringEventSequence), [2, 3])
  const events = (await readState(client)).records.map(record => JSON.parse(record.payloadJson))
  assert.deepEqual(events.map(event => event.event_name), ["host_session", "first", "second"])
  assert.equal(events[0].event_id, result.prefixEventId)
  assert.equal(events[0].event_timestamp, timestamp)
  assert.equal(events[0].event_params.marker, 7)
  assert.equal(events[1].event_params.sequence, 2)
  assert.equal(events[1].user_properties.tier, "mapped")
  assert.equal(events[1].traffic_source.source, "host")
  assert.equal(events[1].geo.country, "KG")
  assert.equal(events[1].device.locale, "en"); assert.equal(events[1].device.platform, "web")
  assert.equal(new Set(events.map(event => event.user.session_id)).size, 1)
})

test("sessions rotate strictly after 30 minutes, and withdrawal preserves installation identity", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client)
  const first = await readState(client)
  h.advance(1800000); await stored(client)
  assert.equal((await readState(client)).meta.session.id, first.meta.session.id)
  h.advance(1800001); await stored(client)
  assert.notEqual((await readState(client)).meta.session.id, first.meta.session.id)
  assert.equal((await client.withdrawConsent()).globalWithdrawalCommitted, true)
  const withdrawn = await readState(client)
  assert.equal(withdrawn.meta.installationId, first.meta.installationId)
  assert.equal(withdrawn.meta.session.id, null); assert.equal(withdrawn.records.length, 0)
  await grant(client); await stored(client)
  const resumed = await readState(client)
  assert.equal(resumed.meta.installationId, first.meta.installationId)
  assert.notEqual(resumed.meta.session.id, first.meta.session.id)
})

test("invalid batches and invalid hook outputs are all-or-nothing", async t => {
  const h = makeHarness(t)
  let mode = "prefix", calls = 0
  const client = await h.create({ sessionStartFactory() { calls++; return { eventName: mode === "prefix" ? "" : "prefix" } },
    eventContextMapper() { return mode === "mapper" ? { eventName: "not allowed" } : null } })
  await grant(client)
  assert.equal((await client.trackBatch([{ eventName: "valid" }, { eventName: "" }])).status, "invalidEvent")
  assert.equal(calls, 0)
  for (mode of ["prefix", "mapper"]) {
    assert.equal((await client.track({ eventName: "valid" })).status, "invalidEvent")
    const state = await readState(client)
    assert.equal(state.records.length, 0); assert.equal(state.meta.session.id, null)
    assert.equal(state.meta.sequence, 0); assert.equal(state.meta.queueBytes, 0)
  }
})

test("hook reentrancy is rejected without altering the admission or permission", async t => {
  const h = makeHarness(t), reentrant = []
  const client = await h.create({ sessionStartFactory() {
    reentrant.push(client.track({ eventName: "nested" }), client.withdrawConsent(),
      client.confirmConsent(() => true), client.flush(), client.validateAndResume({}))
    return null
  } })
  await grant(client); await stored(client)
  for (const result of await Promise.all(reentrant)) {
    assert.equal(result.status, "invalidEvent"); assert.equal(result.safeDiagnosticCode, "callbackReentrancy")
  }
  assert.equal(client.hasCurrentConsent, true)
  assert.equal((await readState(client)).records.length, 1)
})

test("concurrent distinct tabs share installation, session and exactly one committed prefix", async t => {
  const h = makeHarness(t), cfg = { sessionStartFactory: () => ({ eventName: "prefix" }) }
  const [a, b] = await Promise.all([h.create(cfg), h.create(cfg, h.tab())])
  await Promise.all([grant(a), grant(b)])
  await Promise.all([stored(a, { eventName: "tab-a" }), stored(b, { eventName: "tab-b" })])
  const state = await readState(a), other = await readState(b)
  assert.equal(state.meta.installationId, other.meta.installationId)
  const events = state.records.map(record => JSON.parse(record.payloadJson))
  assert.equal(events.filter(event => event.event_name === "prefix").length, 1)
  assert.equal(events.length, 3)
  assert.equal(new Set(events.map(event => event.user.user_pseudo_id)).size, 1)
  assert.equal(new Set(events.map(event => event.user.session_id)).size, 1)
  assert.equal(state.meta.session.counter, 3)
})

test("permission providers fail closed for throw, reject and nonboolean results", async t => {
  const h = makeHarness(t), client = await h.create()
  for (const provider of [() => { throw new Error("private-provider-data") },
    () => Promise.reject(new Error("private-provider-data")), () => "true", () => undefined]) {
    await grant(client)
    const result = await bounded(client.confirmConsent(provider))
    assert.equal(result.status, "consentUnknown"); assert.equal(client.hasCurrentConsent, false)
    assert.equal((await client.track({ eventName: "blocked" })).status, "consentUnknown")
    assert.equal(JSON.stringify(result).includes("private-provider-data"), false)
  }
  assert.equal(h.requests.length, 0)
})

test("a held permission decision cannot grant after another tab withdraws", async t => {
  const h = makeHarness(t), a = await h.create(), b = await h.create({}, h.tab())
  const answer = deferred(), entered = deferred()
  const pending = a.confirmConsent(() => { entered.resolve(); return answer.promise })
  await bounded(entered.promise)
  assert.equal(h.locks.held(`${a.scope}:state`), false)
  assert.equal((await bounded(b.withdrawConsent())).status, "withdrawn")
  answer.resolve(true)
  assert.equal((await bounded(pending)).status, "consentUnknown")
  assert.equal(a.hasCurrentConsent, false)
  await grant(a); await stored(a)
  await b.withdrawConsent()
  assert.equal((await a.track({ eventName: "stale" })).status, "consentUnknown")
  assert.equal((await readState(b)).records.length, 0)
})

test("overlapping local permission checks cannot resurrect an older decision", async t => {
  const h = makeHarness(t), client = await h.create(), held = deferred(), entered = deferred()
  const old = client.confirmConsent(() => { entered.resolve(); return held.promise })
  await bounded(entered.promise)
  assert.equal((await client.confirmConsent(() => false)).status, "withdrawn")
  held.resolve(true)
  assert.equal((await bounded(old)).status, "consentUnknown")
  assert.equal(client.hasCurrentConsent, false)
})

test("a stale held denial cannot purge events admitted after another tab's withdrawal", async t => {
  const h = makeHarness(t), a = await h.create(), b = await h.create({}, h.tab())
  const answer = deferred(), entered = deferred()
  const pending = a.confirmConsent(() => { entered.resolve(); return answer.promise })
  await bounded(entered.promise)
  await b.withdrawConsent(); await grant(b); await stored(b, { eventName: "new-grant-event" })
  answer.resolve(false)
  const result = await bounded(pending)
  assert.notEqual(result.status, "withdrawn")
  assert.equal(result.globalWithdrawalCommitted, false)
  const state = await readState(b)
  assert.deepEqual(state.records.map(record => JSON.parse(record.payloadJson).event_name), ["new-grant-event"])
})

test("fetch is invoked under the state gate, but a late ACK cannot erase post-withdrawal events", async t => {
  const h = makeHarness(t), a = await h.create(), b = await h.create({}, h.tab())
  await grant(a); await stored(a, { eventName: "old" })
  const response = deferred(), started = deferred()
  h.setFetch((_url, init) => {
    assert.equal(h.locks.held(`${a.scope}:state`), true)
    assert.equal(h.locks.held(`${a.scope}:sender`), true)
    assert.equal(init.keepalive, false); assert.equal(init.redirect, "error")
    assert.equal(init.credentials, "omit"); assert.equal(init.referrerPolicy, "no-referrer")
    started.resolve(); return response.promise
  })
  const sending = a.flush()
  await bounded(started.promise)
  try {
    assert.equal((await bounded(b.withdrawConsent())).status, "withdrawn")
    await grant(b); await stored(b, { eventName: "new" })
  } finally { response.resolve(collectorResponse()) }
  assert.notEqual((await bounded(sending)).status, "sent")
  const events = (await readState(b)).records.map(record => JSON.parse(record.payloadJson))
  assert.deepEqual(events.map(event => event.event_name), ["new"])
})

test("same-window duplicate initialization rejects and disposal permits reopening", async t => {
  const h = makeHarness(t), a = await h.create()
  await assert.rejects(h.create(), error => error.code === "alreadyInitialized")
  const otherScope = await h.create({ environment: "another-test" })
  await grant(a); await stored(a)
  a.dispose()
  const reopened = await h.create()
  assert.equal(reopened.hasCurrentConsent, false)
  assert.equal((await readState(reopened)).records.length, 1)
  assert.equal((await readState(otherScope)).records.length, 0)
})

test("endpoint migration requires the exact previous endpoint, preserves bytes and requires a new grant", async t => {
  const h = makeHarness(t), a = await h.create()
  await grant(a); await stored(a)
  const before = await readState(a)
  for (const previousCollectorUrl of [undefined, "https://wrong.example"]) {
    await assert.rejects(h.create({ collectorUrl: "https://new.example", previousCollectorUrl }, h.tab()),
      error => error.code === "endpointMigrationRequired")
  }
  const b = await h.create({ collectorUrl: "https://new.example", previousCollectorUrl: h.cfg.collectorUrl }, h.tab())
  const after = await readState(b)
  assert.equal(after.meta.installationId, before.meta.installationId)
  assert.equal(after.meta.session.id, before.meta.session.id)
  assert.deepEqual(after.records, before.records)
  assert.equal(after.meta.endpointRevision, before.meta.endpointRevision + 1)
  assert.equal(b.hasCurrentConsent, false)
  assert.equal((await a.track({ eventName: "old-endpoint" })).status, "consentUnknown")
  await grant(b)
  assert.equal((await b.flush()).status, "empty")
  assert.equal(h.requests.length, 1)
  assert.equal(h.requests[0].url, "https://new.example/v1/events:batch")
})

test("pending operation quotas reject busy work while an exclusive gate is held", async t => {
  const h = makeHarness(t), client = await h.create({ maxPendingTrackOperations: 1 })
  await grant(client)
  const release = deferred(), entered = deferred()
  const holding = h.locks.request(`${client.scope}:state`, {}, async () => { entered.resolve(); await release.promise })
  await bounded(entered.promise)
  const pending = client.track({ eventName: "first" })
  try { assert.equal((await client.track({ eventName: "second" })).status, "busy") }
  finally { release.resolve() }
  await bounded(holding)
  assert.equal((await bounded(pending)).status, "durablyStored")
  assert.equal((await client.getDiagnostics()).pendingOperations, 0)
})

test("queue limits evict oldest complete records and oversized operations admit nothing", async t => {
  const h = makeHarness(t), client = await h.create({ maxQueueBytes: 2400 })
  await grant(client)
  for (let i = 0; i < 6; i++) await stored(client, { eventName: `event-${i}` })
  const before = await readState(client)
  assert.ok(before.meta.queueBytes <= 2400)
  assert.ok(before.meta.counters.evicted > 0)
  assert.equal(JSON.parse(before.records.at(-1).payloadJson).event_name, "event-5")
  assert.equal((await client.track({ eventName: "huge", eventParams: { body: "x".repeat(3000) } })).status, "invalidEvent")
  assert.deepEqual((await readState(client)).records, before.records)
})

test("pending byte quotas reject overlapping detached snapshots and release reservations", async t => {
  const h = makeHarness(t), client = await h.create({ maxPendingTrackBytes: 4000 })
  await grant(client)
  const release = deferred(), entered = deferred()
  const holding = h.locks.request(`${client.scope}:state`, {}, async () => { entered.resolve(); await release.promise })
  await bounded(entered.promise)
  const input = { eventName: "bounded", eventParams: { value: "x".repeat(2100) } }
  const pending = client.track(input)
  try { assert.equal((await client.track(input)).status, "busy") }
  finally { release.resolve() }
  await bounded(holding)
  assert.equal((await bounded(pending)).status, "durablyStored")
  assert.equal((await client.getDiagnostics()).pendingBytes, 0)
  await stored(client, input)
})

test("async and throwing factory/mapper hooks invalidate the entire admission", async t => {
  for (const hook of ["sessionStartFactory", "eventContextMapper"]) {
    for (const callback of [() => Promise.reject(new Error("private async hook failure")),
      () => { throw new Error("private sync hook failure") }]) {
      await t.test(hook, async t => {
        const h = makeHarness(t), client = await h.create({ [hook]: callback })
        await grant(client)
        assert.equal((await bounded(client.trackBatch([{ eventName: "first" }, { eventName: "second" }]))).status, "invalidEvent")
        const state = await readState(client)
        assert.equal(state.records.length, 0); assert.equal(state.meta.session.id, null)
        assert.equal(state.meta.sequence, 0)
      })
    }
  }
})

test("storage failures fail closed and never expose raw exception data", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client)
  client.store._db.transaction = () => { throw new DOMException("secret quota details", "QuotaExceededError") }
  const result = await bounded(client.track({ eventName: "blocked" }))
  assert.equal(result.status, "storageFailure")
  assert.equal(client.hasCurrentConsent, false)
  const diagnostics = await bounded(client.getDiagnostics())
  assert.equal(diagnostics.storageAvailable, false)
  assert.equal(JSON.stringify({ result, diagnostics }).includes("secret quota details"), false)
  assert.equal(h.requests.length, 0)
})

test("unsupported storage fails initialization without falling back to memory", async t => {
  const h = makeHarness(t)
  const runtime = { ...h.runtime, indexedDB: { open() { throw new Error("private open failure") } } }
  await assert.rejects(bounded(createBrowserGameClient({ ...h.cfg, runtime })), error => {
    assert.equal(error.code, "storageFailure")
    assert.equal(error.message.includes("private open failure"), false)
    return true
  })
  const client = await h.create()
  assert.equal((await readState(client)).records.length, 0)
})

test("diagnostics contain counts and safe codes, not identities, API keys or payloads", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client)
  await stored(client, { eventName: "private-event-name", eventId: "private-event-id",
    user: { user_id: "private-account-id" }, eventParams: { body: "private-payload-content" } })
  const state = await readState(client), serialized = JSON.stringify(await client.getDiagnostics())
  for (const secret of [h.cfg.apiKey, state.meta.installationId, state.meta.session.id, state.meta.storeInstanceId,
    state.records[0].handle, "private-event-name", "private-event-id", "private-account-id", "private-payload-content", "payloadJson"]) {
    assert.equal(serialized.includes(secret), false, `diagnostics must exclude ${secret}`)
  }
})
