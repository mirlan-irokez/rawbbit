import test from "node:test"
import assert from "node:assert/strict"
import { makeHarness, grant, stored, readState, deferred, recoveryOptions, collectorResponse } from "./helpers.js"
import { detachJson } from "../payload.js"

test("failed withdrawal blocks a later grant until its durable barrier is repaired", async t => {
  const h = makeHarness(t)
  const client = await h.create({ lockTimeoutMs: 10 })
  await grant(client); await stored(client)
  const release = deferred(), held = deferred()
  const holder = h.locks.request(`${client.scope}:state`, async () => { held.resolve(); await release.promise })
  await held.promise
  const withdrawal = await client.withdrawConsent()
  assert.equal(withdrawal.globalWithdrawalCommitted, false)
  assert.equal(withdrawal.localDisabled, true)
  assert.equal((await client.confirmConsent(() => true)).safeDiagnosticCode, "withdrawalIncomplete")
  release.resolve(); await holder
  assert.equal((await readState(client)).records.length, 1)
  assert.equal((await client.withdrawConsent()).globalWithdrawalCommitted, true)
  await grant(client)
  assert.equal((await readState(client)).records.length, 0)
})

test("a response body deadline is transient, not malformed-success recovery", async t => {
  const h = makeHarness(t)
  const client = await h.create({ timeoutMs: 10 })
  h.setFetch(() => new Response(new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('{"accepted_events":'))
  } }), { status: 200 }))
  await grant(client); await stored(client)
  assert.equal((await client.flush()).status, "pending")
  const state = await readState(client)
  assert.equal(state.records.length, 1)
  assert.equal(state.meta.fault, null)
  assert.equal(state.meta.retry.attempt, 1)
})

test("snapshot detachment rejects unbounded strings and object-key data before cloning", () => {
  assert.throws(() => detachJson({ value: "x".repeat(1000) }, 32, 100), /size exceeded/)
  assert.throws(() => detachJson({ ["x".repeat(1000)]: 1 }, 32, 100), /size exceeded/)
  assert.deepEqual(detachJson({ value: "small" }, 32, 100), { value: "small" })
})

test("recovery snapshots the selected handles and reviewed policy before a held gate", async t => {
  const h = makeHarness(t)
  const client = await h.create()
  await grant(client); await stored(client, [{ eventName: "first" }, { eventName: "second" }])
  h.setFetch(() => collectorResponse(400, {}))
  await client.flush()
  const rows = await client.listQuarantined()
  const handles = [rows[0].handle]
  const options = await recoveryOptions(client, { reasonCode: "invalidFrozenEvent" })
  const release = deferred(), held = deferred()
  const holder = h.locks.request(`${client.scope}:state`, async () => { held.resolve(); await release.promise })
  await held.promise
  const discard = client.discardQuarantined(handles, options)
  handles[0] = rows[1].handle
  options.reasonCode = "unreviewed"
  options.policyVersion = "x".repeat(2000)
  release.resolve(); await holder
  assert.equal((await discard).status, "recovered")
  const state = await readState(client)
  assert.deepEqual(state.records.map(record => record.handle), [rows[1].handle])
  assert.equal(state.receipts.at(-1).reasonCode, "invalidFrozenEvent")
  assert.equal(state.receipts.at(-1).policyVersion, "test-policy-v1")
})

test("a held recovery probe retains its call-time policy instead of mutated options", async t => {
  const h = makeHarness(t)
  const client = await h.create()
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(401, {})); await client.flush()
  const options = await recoveryOptions(client)
  const release = deferred(), held = deferred()
  const holder = h.locks.request(`${client.scope}:state`, async () => { held.resolve(); await release.promise })
  await held.promise
  h.setFetch(() => collectorResponse(200, { accepted_events: 1 }))
  const recovery = client.validateAndResume(options)
  options.reasonCode = "unreviewed"
  options.policyVersion = "x".repeat(2000)
  release.resolve(); await holder
  assert.equal((await recovery).status, "resumed")
  const state = await readState(client)
  assert.equal(state.receipts.at(-1).reasonCode, "configurationCorrected")
  assert.equal(state.receipts.at(-1).policyVersion, "test-policy-v1")
})

test("prefix and triggering adapters receive their own contiguous event sequences", async t => {
  const h = makeHarness(t)
  const client = await h.create({ sessionStartFactory: context => ({ eventName: "prefix", eventParams: { sequence: context.eventSequence } }),
    eventContextMapper: context => ({ eventParams: { sequence: context.eventSequence } }) })
  await grant(client); await stored(client)
  assert.deepEqual((await readState(client)).records.map(record => JSON.parse(record.payloadJson).event_params.sequence), [1, 2])
})

test("scalar mapper results fail the whole admission instead of silently becoming no-ops", async t => {
  for (const mapping of [42, true, "", []]) {
    const h = makeHarness(t)
    const client = await h.create({ eventContextMapper: () => mapping })
    await grant(client)
    assert.equal((await client.track({ eventName: "action" })).status, "invalidEvent")
    assert.equal((await readState(client)).records.length, 0)
  }
})

test("termination after reservation but before fetch preserves the unused attempt and deadline", async t => {
  const h = makeHarness(t)
  const client = await h.create()
  await grant(client); await stored(client)
  const update = client.store.update.bind(client.store)
  client.store.update = async mutator => {
    const value = await update(mutator)
    // Simulate local suspension immediately after claim transaction completion.
    client._invalidate()
    return value
  }
  assert.equal((await client.flush()).safeDiagnosticCode, "stalePreparation")
  assert.equal(h.requests.length, 0)
  client.store.update = update
  const reserved = await readState(client)
  assert.equal(reserved.meta.retry.attempt, 1)
  assert.equal(reserved.meta.claim.attempt, 1)
  client.dispose()
  const next = await h.create()
  await grant(next)
  assert.equal((await next.flush()).status, "pending")
  assert.equal(h.requests.length, 0)
  h.now = reserved.meta.retry.notBefore
  assert.equal((await next.flush()).status, "empty")
  assert.equal(h.requests.length, 1)
})

test("loss of completion commit after fetch retains exact events and crash retry budget", async t => {
  const h = makeHarness(t)
  const client = await h.create()
  await grant(client); const tracked = await stored(client)
  const update = client.store.update.bind(client.store)
  h.setFetch(() => {
    client.store.update = () => Promise.reject(Object.assign(new Error("synthetic completion interruption"), { code: "storageFailure" }))
    return collectorResponse(200, { accepted_events: 1 })
  })
  assert.equal((await client.flush()).status, "storageFailure")
  client.store.update = update
  const reserved = await readState(client)
  assert.equal(reserved.records.length, 1)
  assert.equal(reserved.meta.retry.attempt, 1)
  assert.equal(reserved.meta.claim.attempt, 1)
  client.dispose()
  const next = await h.create()
  await grant(next)
  assert.equal((await next.flush()).status, "pending")
  assert.equal(h.requests.length, 1)
  h.now = reserved.meta.retry.notBefore
  h.setFetch(() => collectorResponse(200, { accepted_events: 1 }))
  assert.equal((await next.flush()).status, "empty")
  assert.deepEqual(h.requests.map(request => request.events[0].event_id), [tracked.eventIds[0], tracked.eventIds[0]])
})

test("an obsolete denial does not require repair or purge a newer epoch before fresh confirmation", async t => {
  const h = makeHarness(t)
  const first = await h.create()
  const second = await h.create({}, h.tab())
  const decision = deferred(), checked = deferred()
  const checking = first.confirmConsent(() => { checked.resolve(); return decision.promise })
  await checked.promise
  await second.withdrawConsent()
  await grant(second); await stored(second, { eventName: "new_epoch" })
  decision.resolve(false)
  assert.equal((await checking).safeDiagnosticCode, "consentChanged")
  assert.equal((await first.getDiagnostics()).withdrawalIncomplete, false)
  await grant(first)
  assert.equal((await readState(first)).records.length, 1)
})

test("configured JSON depth above 32 is honored through every adapter copy", async t => {
  let deep = { value: true }
  for (let depth = 0; depth < 40; depth++) deep = { nested: deep }
  for (const hooks of [{ eventContextMapper: () => null }, { sessionStartFactory: () => null },
    { sessionStartFactory: () => ({ eventName: "prefix", eventParams: { deep } }), eventContextMapper: () => ({ eventParams: { deep } }) }]) {
    const h = makeHarness(t)
    const client = await h.create({ maxSnapshotDepth: 64, ...hooks })
    await grant(client)
    assert.equal((await client.track({ eventName: "deep", eventParams: { deep } })).status, "durablyStored")
  }
})

test("obsolete denial cleanup cannot clear a newer failed explicit withdrawal", async t => {
  const h = makeHarness(t)
  const client = await h.create({ lockTimeoutMs: 10 })
  await grant(client); await stored(client, { eventName: "must_purge" })
  const update = client.store.update.bind(client.store)
  const entered = deferred(), release = deferred()
  client.store.update = async mutator => { entered.resolve(); await release.promise; return update(mutator) }
  const oldDenial = client.confirmConsent(() => false)
  await entered.promise
  const newerWithdrawal = await client.withdrawConsent()
  assert.equal(newerWithdrawal.globalWithdrawalCommitted, false)
  assert.equal(newerWithdrawal.status, "busy")
  release.resolve()
  await oldDenial
  client.store.update = update
  assert.equal((await client.getDiagnostics()).withdrawalIncomplete, true)
  assert.equal((await client.confirmConsent(() => true)).safeDiagnosticCode, "withdrawalIncomplete")
  assert.equal(h.requests.length, 0)
  assert.equal((await readState(client)).records.length, 1)
  assert.equal((await client.withdrawConsent()).globalWithdrawalCommitted, true)
  await grant(client)
  assert.equal((await readState(client)).records.length, 0)
})
