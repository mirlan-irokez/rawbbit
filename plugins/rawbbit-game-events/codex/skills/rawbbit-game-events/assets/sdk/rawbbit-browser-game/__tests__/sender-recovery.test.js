import test from "node:test"
import assert from "node:assert/strict"
import { makeHarness, bounded, deferred, collectorResponse, grant, stored, readState, recoveryOptions } from "./helpers.js"

test("normal delivery requires an exact accepted_events count and preserves frozen bytes", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client)
  await stored(client, [{ eventName: "first" }, { eventName: "second" }])
  const before = await readState(client)
  assert.equal((await bounded(client.flush())).status, "empty")
  assert.equal(h.requests.length, 1)
  assert.equal(h.requests[0].init.body, `{"events":[${before.records.map(record => record.payloadJson).join(",")}]}`)
  const after = await readState(client)
  assert.equal(after.records.length, 0); assert.equal(after.meta.queueBytes, 0)
  assert.deepEqual(after.meta.retry, { attempt: 0, notBefore: 0 })
})

test("malformed, missing, noninteger and mismatched accepted counts latch a fault without deleting records", async t => {
  for (const body of [{ accepted_events: 0 }, { accepted_events: 2 }, { accepted_events: "1" },
    { accepted_events: 1.5 }, { accepted_events: -1 }, {}, null, "not-json"]) {
    await t.test(JSON.stringify(body), async t => {
      const h = makeHarness(t), client = await h.create()
      h.setFetch(() => collectorResponse(200, body))
      await grant(client); await stored(client)
      const before = await readState(client)
      assert.equal((await bounded(client.flush())).status, "paused")
      const after = await readState(client)
      assert.deepEqual(after.records, before.records)
      assert.equal(after.meta.fault.code, "acceptedCountMismatch")
      assert.equal((await client.flush()).status, "paused")
      assert.equal(h.requests.length, 1)
    })
  }
})

test("an oversized or unreadable success body is not an acknowledgment", async t => {
  for (const fetcher of [() => collectorResponse(200, "x".repeat(100)),
    () => ({ ok: true, status: 200, headers: new Headers(), body: new ReadableStream({
      start(controller) { controller.error(new Error("private body error")) },
    }) })]) {
    await t.test("body failure", async t => {
      const h = makeHarness(t), client = await h.create({ maxResponseBytes: 32 })
      h.setFetch(fetcher); await grant(client); await stored(client)
      assert.equal((await bounded(client.flush())).status, "paused")
      assert.equal((await client.getDiagnostics()).faultCode, "acceptedCountMismatch")
      assert.equal((await readState(client)).records.length, 1)
    })
  }
})

test("nonretryable HTTP responses latch safe faults; only payload rejection quarantines", async t => {
  for (const status of [400, 401, 403, 404, 422, 302]) {
    await t.test(String(status), async t => {
      const h = makeHarness(t), client = await h.create()
      h.setFetch(() => collectorResponse(status, { error: "private collector error" }))
      await grant(client); await stored(client)
      assert.equal((await bounded(client.flush())).status, "paused")
      const diagnostics = await client.getDiagnostics()
      assert.equal(diagnostics.lastHttpStatus, status)
      assert.equal(diagnostics.recoveryProbeRequired, true)
      assert.equal(diagnostics.quarantinedCount, [400, 422].includes(status) ? 1 : 0)
      assert.equal(JSON.stringify(diagnostics).includes("private collector error"), false)
      await client.flush(); assert.equal(h.requests.length, 1)
    })
  }
})

test("408, 429, 5xx and network failures reserve a durable retry budget", async t => {
  for (const status of [408, 429, 500, 503, 0]) {
    await t.test(String(status), async t => {
      const h = makeHarness(t), client = await h.create()
      h.setFetch(() => status ? collectorResponse(status, {}) : Promise.reject(new Error("private network error")))
      await grant(client); await stored(client)
      for (const [attempt, delay] of [[1, 1], [2, 2], [3, 10], [1, 1]]) {
        const result = await bounded(client.flush())
        assert.equal(result.status, "pending")
        const diagnostics = await client.getDiagnostics()
        assert.equal(diagnostics.retryAttempt, attempt)
        assert.equal(diagnostics.retryNotBefore, h.now + delay)
        assert.equal(diagnostics.faultCode, null)
        const calls = h.requests.length
        assert.equal((await client.flush()).safeDiagnosticCode, "retryCooldown")
        assert.equal(h.requests.length, calls)
        h.advance(delay)
      }
      assert.equal((await readState(client)).records.length, 1)
    })
  }
})

test("readable Retry-After seconds and dates are bounded and persist across reopening", async t => {
  for (const kind of ["seconds", "date", "bounded", "unreadable"]) {
    await t.test(kind, async t => {
      const h = makeHarness(t), client = await h.create()
      const raw = kind === "seconds" ? "2" : kind === "date" ? new Date(h.now + 3000).toUTCString() : "999999999999"
      h.setFetch(() => {
        const response = collectorResponse(429, {}, { "Retry-After": raw })
        if (kind === "unreadable") Object.defineProperty(response, "headers", { value: { get() { throw new Error("CORS-hidden header") } } })
        return response
      })
      await grant(client); await stored(client); await client.flush()
      const delay = kind === "seconds" ? 2000 : kind === "date" ? 3000 : kind === "bounded" ? 86400000 : 1
      const reserved = await readState(client)
      assert.equal(reserved.meta.retry.notBefore, h.now + delay)
      client.dispose()
      const reopened = await h.create()
      await grant(reopened)
      assert.equal((await reopened.flush()).safeDiagnosticCode, "retryCooldown")
      assert.equal(h.requests.length, 1)
      assert.deepEqual((await readState(reopened)).meta.retry, reserved.meta.retry)
      h.advance(delay)
      h.setFetch((_url, init) => collectorResponse(200, { accepted_events: JSON.parse(init.body).events.length }))
      assert.equal((await reopened.flush()).status, "empty")
    })
  }
})

test("an abandoned in-flight claim retains its reserved deadline and attempt after dispose/reopen", async t => {
  const h = makeHarness(t), client = await h.create(), response = deferred(), started = deferred()
  h.setFetch(() => { started.resolve(); return response.promise })
  await grant(client); await stored(client)
  const sending = client.flush()
  await bounded(started.promise)
  let reserved
  try {
    reserved = await readState(client)
    assert.ok(reserved.meta.claim)
    assert.equal(reserved.meta.claim.attempt, 1)
    assert.equal(reserved.meta.retry.notBefore, h.now + 101)
    client.dispose()
  } finally { response.resolve(collectorResponse()) }
  await bounded(sending)
  const reopened = await h.create()
  await grant(reopened)
  assert.equal((await reopened.flush()).safeDiagnosticCode, "retryCooldown")
  const takeover = await readState(reopened)
  assert.equal(takeover.meta.claim, null)
  assert.deepEqual(takeover.meta.retry, reserved.meta.retry)
  assert.equal(takeover.records.length, 1)
  assert.equal(h.requests.length, 1)
  h.now = reserved.meta.retry.notBefore
  h.setFetch(() => collectorResponse())
  assert.equal((await reopened.flush()).status, "empty")
  assert.equal(h.requests.length, 2)
})

test("multi-event 413 splits the frozen batch, while a single-event 413 quarantines", async t => {
  const h = makeHarness(t), client = await h.create()
  h.setFetch((_url, init) => {
    const count = JSON.parse(init.body).events.length
    return count > 2 ? collectorResponse(413, {}) : collectorResponse(200, { accepted_events: count })
  })
  await grant(client)
  await stored(client, Array.from({ length: 4 }, (_, i) => ({ eventName: `event-${i}` })))
  const before = await readState(client)
  assert.equal((await bounded(client.flush())).status, "empty")
  assert.deepEqual(h.requests.map(request => request.events.length), [4, 2, 2])
  assert.deepEqual(h.requests.slice(1).flatMap(request => request.events.map(event => event.event_id)),
    before.records.map(record => JSON.parse(record.payloadJson).event_id))
  await stored(client, { eventName: "single" })
  h.setFetch(() => collectorResponse(413, {}))
  assert.equal((await client.flush()).status, "paused")
  const diagnostics = await client.getDiagnostics()
  assert.equal(diagnostics.quarantinedCount, 1)
  assert.equal(diagnostics.faultCode, "collectorHttp413")
})

test("retention removes expired records at the exact boundary, but never clears the fault latch", async t => {
  const h = makeHarness(t), client = await h.create({ retentionDays: 10 / 86400000 })
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(400, {})); await client.flush()
  const fault = await client.getDiagnostics()
  h.advance(10)
  assert.equal((await client.flush()).status, "paused")
  const expired = await client.getDiagnostics()
  assert.equal(expired.pendingCount, 0); assert.equal(expired.quarantinedCount, 0)
  assert.equal(expired.counters.expired, 1); assert.equal(expired.counters.quarantinedExpired, 1)
  assert.equal(expired.faultCode, fault.faultCode)
  assert.equal(expired.faultRevision, fault.faultRevision)
  h.advance(100)
  assert.equal((await client.validateAndResume(await recoveryOptions(client))).safeDiagnosticCode, "probeEventRequired")
  assert.equal(h.requests.length, 1)
})

test("quota eviction accounts for quarantined records without automatically resuming delivery", async t => {
  const h = makeHarness(t), client = await h.create({ maxQueueBytes: 2400 })
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(422, {})); await client.flush()
  for (let i = 0; i < 5; i++) await stored(client, { eventName: `fresh-${i}` })
  const diagnostics = await client.getDiagnostics()
  assert.ok(diagnostics.queueBytes <= 2400)
  assert.equal(diagnostics.quarantinedCount, 0)
  assert.equal(diagnostics.counters.quarantinedEvicted, 1)
  assert.equal(diagnostics.faultCode, "collectorHttp422")
  assert.equal((await client.flush()).status, "paused")
  assert.equal(h.requests.length, 1)
})

test("retry/discard require current revisions, approved reasons and a bounded policy version", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(400, {})); await client.flush()
  const quarantined = await client.listQuarantined(), handles = quarantined.map(record => record.handle)
  assert.deepEqual(Object.keys(quarantined[0]).sort(), ["enqueuedAt", "faultCode", "handle", "httpStatus"])
  const before = await readState(client), options = await recoveryOptions(client)
  for (const invalid of [{}, { ...options, reasonCode: "arbitrary" }, { ...options, policyVersion: "bad policy" },
    { ...options, policyVersion: "x".repeat(65) }]) {
    assert.equal((await client.retryQuarantined(handles, invalid)).status, "invalidEvent")
  }
  for (const invalid of [{ ...options, expectedFaultRevision: options.expectedFaultRevision + 1 },
    { ...options, expectedQueueRevision: options.expectedQueueRevision + 1 }]) {
    assert.equal((await client.retryQuarantined(handles, invalid)).status, "recoveryPending")
  }
  assert.equal((await client.retryQuarantined([...handles, ...handles], options)).status, "invalidEvent")
  assert.equal((await client.retryQuarantined(["unknown-handle"], options)).status, "recoveryPending")
  assert.equal((await client.discardQuarantined(handles, options)).status, "invalidEvent")
  assert.deepEqual((await readState(client)).records, before.records)
  assert.equal(h.requests.length, 1)
  assert.equal((await client.retryQuarantined(handles, options)).status, "recovered")
  const retried = await readState(client)
  assert.equal(retried.records[0].payloadJson, before.records[0].payloadJson)
  assert.equal(retried.records[0].quarantined, false)
  assert.ok(retried.meta.fault)
  assert.equal((await client.flush()).status, "paused")
  h.advance(200)
  h.setFetch(() => collectorResponse())
  assert.equal((await client.validateAndResume(await recoveryOptions(client))).status, "resumed")
  assert.equal((await client.getDiagnostics()).faultCode, null)
  assert.equal((await readState(client)).records.length, 0)
})

test("discard is explicit, auditable and cannot synthesize a validation probe", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client, { eventName: "rejected-private-event" })
  h.setFetch(() => collectorResponse(422, {})); await client.flush()
  const handles = (await client.listQuarantined()).map(record => record.handle)
  const options = await recoveryOptions(client, { reasonCode: "collectorContractRejected" })
  assert.equal((await client.discardQuarantined(handles, options)).status, "recovered")
  const discarded = await readState(client)
  assert.equal(discarded.records.length, 0); assert.equal(discarded.meta.counters.discarded, 1)
  const receipt = discarded.receipts.find(receipt => receipt.operation === "discard")
  assert.equal(receipt.count, 1); assert.equal(receipt.reasonCode, "collectorContractRejected")
  assert.equal(JSON.stringify(receipt).includes("rejected-private-event"), false)
  h.advance(200)
  assert.equal((await client.validateAndResume(await recoveryOptions(client))).safeDiagnosticCode, "probeEventRequired")
  assert.equal(h.requests.length, 1)
  await stored(client, { eventName: "host-validation-event" })
  h.setFetch(() => collectorResponse())
  assert.equal((await client.validateAndResume(await recoveryOptions(client, { reasonCode: "postDisposalValidation" }))).status, "resumed")
  assert.equal(h.requests[1].events.length, 1)
  assert.equal(h.requests[1].events[0].event_name, "host-validation-event")
  assert.equal((await client.getDiagnostics()).faultCode, null)
})

test("validation requires revisions and policy, and blocks unresolved quarantine", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(400, {})); await client.flush()
  const options = await recoveryOptions(client)
  for (const invalid of [{}, { ...options, expectedFaultRevision: undefined }, { ...options, expectedQueueRevision: 1.5 },
    { ...options, reasonCode: "collectorContractRejected" }, { ...options, policyVersion: "" }]) {
    assert.equal((await client.validateAndResume(invalid)).status, "invalidEvent")
  }
  assert.equal((await client.validateAndResume({ ...options, expectedFaultRevision: options.expectedFaultRevision + 1 })).safeDiagnosticCode, "staleRecovery")
  assert.equal((await client.validateAndResume(options)).safeDiagnosticCode, "unresolvedQuarantine")
  assert.equal(h.requests.length, 1)
})

test("negative recovery revisions are invalid input rather than stale selections", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(400, {})); await client.flush()
  const options = await recoveryOptions(client)
  for (const revision of ["expectedFaultRevision", "expectedQueueRevision"]) {
    assert.equal((await client.validateAndResume({ ...options, [revision]: -1 })).status, "invalidEvent", revision)
  }
  assert.equal(h.requests.length, 1)
})

test("retry cannot release an invalid frozen payload and discard still requires explicit review", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(400, {})); await client.flush()
  // Simulate an older/broken SDK's stored envelope without violating storage
  // accounting, so the recovery layer must validate the frozen contract itself.
  await bounded(client.coord.state(() => client.store.update(state => {
    const record = state.records[0], event = JSON.parse(record.payloadJson), before = record.bytes
    event.device.platform = "native"
    record.payloadJson = JSON.stringify(event)
    record.bytes = new TextEncoder().encode(record.payloadJson).byteLength + 512
    state.meta.queueBytes += record.bytes - before
    state.meta.queueRevision++
  })))
  const handles = (await client.listQuarantined()).map(record => record.handle)
  assert.equal((await client.retryQuarantined(handles, await recoveryOptions(client))).status, "recoveryPending")
  assert.equal((await client.getDiagnostics()).quarantinedCount, 1)
  assert.equal((await client.validateAndResume(await recoveryOptions(client))).safeDiagnosticCode, "unresolvedQuarantine")
  assert.equal((await client.discardQuarantined(handles, await recoveryOptions(client, { reasonCode: "invalidFrozenEvent" }))).status, "recovered")
  assert.equal((await readState(client)).records.length, 0)
  assert.equal(h.requests.length, 1)
})

test("faults survive empty queues, withdrawal and reconsent until a real eligible event validates", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(401, {})); await client.flush()
  const original = await client.getDiagnostics()
  await client.withdrawConsent(); await grant(client)
  const after = await client.getDiagnostics()
  assert.equal(after.pendingCount, 0)
  assert.equal(after.faultCode, original.faultCode); assert.equal(after.faultRevision, original.faultRevision)
  assert.equal((await client.flush()).status, "paused")
  assert.equal((await client.validateAndResume(await recoveryOptions(client))).safeDiagnosticCode, "probeEventRequired")
  assert.equal(h.requests.length, 1)
  await stored(client, [{ eventName: "probe" }, { eventName: "later" }])
  h.setFetch(() => collectorResponse())
  assert.equal((await client.validateAndResume(await recoveryOptions(client))).status, "resumed")
  assert.equal(h.requests[1].events.length, 1); assert.equal(h.requests[1].events[0].event_name, "probe")
  assert.equal((await client.getDiagnostics()).pendingCount, 1)
  assert.equal((await client.getDiagnostics()).faultCode, null)
  assert.equal((await client.flush()).status, "empty")
})

test("a failed validation ACK never clears the durable fault", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client)
  h.setFetch(() => collectorResponse(401, {})); await client.flush()
  h.advance(200)
  h.setFetch(() => collectorResponse(200, { accepted_events: 0 }))
  assert.equal((await client.validateAndResume(await recoveryOptions(client))).status, "recoveryPending")
  assert.ok((await client.getDiagnostics()).faultCode)
  assert.equal((await readState(client)).records.length, 1)
})

test("a stale in-flight recovery probe cannot clear a fault after the queue revision changes", async t => {
  const h = makeHarness(t), client = await h.create()
  await grant(client); await stored(client, { eventName: "probe" })
  h.setFetch(() => collectorResponse(401, {})); await client.flush()
  h.advance(200)
  const response = deferred(), started = deferred()
  h.setFetch(() => { started.resolve(); return response.promise })
  const validating = client.validateAndResume(await recoveryOptions(client))
  await bounded(started.promise)
  try { await stored(client, { eventName: "unrelated" }) }
  finally { response.resolve(collectorResponse()) }
  assert.equal((await bounded(validating)).safeDiagnosticCode, "staleRecovery")
  assert.ok((await client.getDiagnostics()).faultCode)
  assert.equal((await readState(client)).records.length, 2)
})

test("unrelated enqueue revisions do not invalidate a normal ACK", async t => {
  const h = makeHarness(t), client = await h.create(), response = deferred(), started = deferred()
  await grant(client); await stored(client, { eventName: "in-flight" })
  h.setFetch(() => {
    if (h.requests.length === 1) { started.resolve(); return response.promise }
    return collectorResponse(503, {})
  })
  const flushing = client.flush()
  await bounded(started.promise)
  try { await stored(client, { eventName: "new-event" }) }
  finally { response.resolve(collectorResponse()) }
  assert.equal((await bounded(flushing)).status, "pending")
  const state = await readState(client)
  assert.deepEqual(state.records.map(record => JSON.parse(record.payloadJson).event_name), ["new-event"])
  assert.equal(state.meta.fault, null)
  assert.deepEqual(h.requests.map(request => request.events[0].event_name), ["in-flight", "new-event"])
})

test("flush timeout bounds the caller even when native fetch ignores abort", async t => {
  const h = makeHarness(t), client = await h.create({ timeoutMs: 200 }), response = deferred(), started = deferred()
  await grant(client); await stored(client)
  h.setFetch(() => { started.resolve(); return response.promise })
  const begin = Date.now(), flushing = client.flush({ timeoutMs: 20 })
  await bounded(started.promise)
  try {
    const result = await bounded(flushing, 120)
    assert.equal(result.status, "timeout")
    assert.ok(Date.now() - begin < 150)
    assert.equal(h.locks.held(`${client.scope}:sender`), true, "caller timeout must not steal live sender ownership")
  } finally {
    response.resolve(collectorResponse())
    await bounded(flushing)
  }
})
