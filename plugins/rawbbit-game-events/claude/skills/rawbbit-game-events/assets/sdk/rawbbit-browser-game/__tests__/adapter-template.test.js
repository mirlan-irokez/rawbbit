import test from "node:test"
import assert from "node:assert/strict"
import { readFile, unlink, writeFile } from "node:fs/promises"
import { dirname, join } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { bounded, collectorResponse, deferred, FakeWebLocks, makeHarness } from "./helpers.js"

const root = dirname(dirname(fileURLToPath(import.meta.url)))

function makeGeneration() {
  let value = 0
  return {
    readAnalyticsPermissionGeneration: () => value,
    advance() { value += 1; return value },
  }
}

function delayNthHostLockRequest(lockManager) {
  let hostRequestCount = 0
  let delayedRequest = null
  let targetRequest = null
  let ready = null
  return {
    request(name, options, callback) {
      if (name.endsWith(":host-consent")) {
        hostRequestCount += 1
        if (hostRequestCount === targetRequest) {
          targetRequest = null
          return new Promise((resolve, reject) => {
            const abort = () => {
              if (delayedRequest?.started) return
              delayedRequest = null
              reject(options.signal?.reason ?? new DOMException("Aborted", "AbortError"))
            }
            delayedRequest = {
              started: false,
              start() {
                if (options.signal?.aborted) return abort()
                this.started = true
                options.signal?.removeEventListener("abort", abort)
                lockManager.request(name, options, callback).then(resolve, reject)
              },
            }
            options.signal?.addEventListener("abort", abort, { once: true })
            ready.resolve()
          })
        }
      }
      return lockManager.request(name, options, callback)
    },
    delayNextHostRequest() {
      targetRequest = hostRequestCount + 1
      ready = deferred()
      return ready.promise
    },
    delayFetchRequest() {
      targetRequest = hostRequestCount + 2 // adapter flush gate, then SDK Fetch gate
      ready = deferred()
      return ready.promise
    },
    releaseDelayedRequest() {
      assert.ok(delayedRequest, "expected a delayed Fetch lock request")
      delayedRequest.start()
    },
  }
}

async function loadAdapterTemplate(t) {
  const templatePath = join(root, "examples", "GameAnalyticsAdapter.js.template")
  const tempPath = join(root, "examples", `.adapter-template-test-${process.pid}-${Math.random().toString(16).slice(2)}.mjs`)
  const template = await readFile(templatePath, "utf8")
  await writeFile(tempPath, template.replace('from "@rawbbit/browser-game"', 'from "../index.js"'))
  t.after(() => unlink(tempPath))
  return import(`${pathToFileURL(tempPath).href}?test=${Math.random()}`)
}

test("adapter requires a synchronous host permission-generation reader", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  await assert.rejects(createGameAnalyticsAdapter({
    config: h.cfg,
    readCurrentAnalyticsPermission: () => true,
    isAnalyticsPermissionAllowed: () => true,
    persistAnalyticsPermissionDenied: async () => {},
  }), /readAnalyticsPermissionGeneration must be a synchronous function/)
})

test("adapter persists host denial when SDK withdrawal cannot acquire its lock, then repairs before grant", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let hostPermission = true
  let denialPending = false
  let denialWrites = 0
  const generation = makeGeneration()
  const readCurrentAnalyticsPermission = () => denialPending ? false : hostPermission
  const isAnalyticsPermissionAllowed = () => !denialPending && hostPermission
  const persistAnalyticsPermissionDenied = async () => {
    generation.advance()
    denialWrites += 1
    denialPending = true
    hostPermission = false
    denialPending = false
  }
  const firstView = h.tab(), secondView = h.tab()
  const adapter = await createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: firstView.runtime, lockTimeoutMs: 15 },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  t.after(() => adapter.dispose())
  const peer = await createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: secondView.runtime, lockTimeoutMs: 15 },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  t.after(() => peer.dispose())

  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await peer.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await adapter.trackCommittedTransition("before_withdrawal", {})).status, "durablyStored")

  const entered = deferred(), release = deferred()
  const scope = `rawbbit-browser-game:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const lock = h.locks.request(`${scope}:revocation`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise

  const withdrawal = adapter.withdrawConsent()
  const failed = await bounded(withdrawal)
  assert.equal(failed.globalWithdrawalCommitted, false)
  assert.equal(failed.revocationCleared, false)
  assert.equal(failed.status, "busy")
  assert.equal(denialWrites, 1)
  assert.equal(hostPermission, false)
  assert.equal(h.pendingRevocation, null)
  assert.equal((await peer.getDiagnostics()).pendingCount, 2)
  assert.equal((await peer.trackCommittedTransition("after_failed_withdrawal", {})).status, "consentWithdrawn")
  assert.equal((await peer.flush()).status, "paused")
  assert.equal(h.requests.length, 0)
  release.resolve(); await lock

  adapter.dispose()
  peer.dispose()
  const restartedView = h.tab()
  const restarted = await createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: restartedView.runtime, lockTimeoutMs: 200 },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  t.after(() => restarted.dispose())

  const denied = await bounded(restarted.withdrawConsent())
  assert.equal(denied.globalWithdrawalCommitted, true)
  assert.equal(denied.revocationCleared, true)
  assert.equal(hostPermission, false)
  assert.equal((await restarted.getDiagnostics()).pendingCount, 0)
  assert.equal(h.requests.length, 0)
})

test("adapter confirmation waits for in-flight host denial instead of granting stale permission", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let hostPermission = true
  let denialPending = false
  let permissionReads = 0
  const generation = makeGeneration()
  const persistEntered = deferred(), persistRelease = deferred()
  const adapter = await createGameAnalyticsAdapter({
    config: h.cfg,
    readCurrentAnalyticsPermission: () => { permissionReads += 1; return denialPending ? false : hostPermission },
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => !denialPending && hostPermission,
    persistAnalyticsPermissionDenied: async () => {
      generation.advance()
      denialPending = true
      persistEntered.resolve()
      await persistRelease.promise
      hostPermission = false
      denialPending = false
    },
  })
  t.after(() => adapter.dispose())

  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  await adapter.trackCommittedTransition("before_withdrawal", {})
  permissionReads = 0

  const withdrawal = adapter.withdrawConsent()
  await persistEntered.promise

  // The SDK may have committed the purge while the host's durable deny is
  // still pending. A lifecycle confirmation must not use the stale `true`.
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (h.pendingRevocation === null && (await adapter.getDiagnostics()).pendingCount === 0) break
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal((await adapter.getDiagnostics()).pendingCount, 0)

  let confirmationSettled = false
  const confirmation = adapter.confirmCurrentPermission().then(value => {
    confirmationSettled = true
    return value
  })
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(confirmationSettled, false)
  assert.equal(permissionReads, 0)

  persistRelease.resolve()
  const [withdrawalResult, confirmationResult] = await Promise.all([withdrawal, confirmation])
  assert.equal(withdrawalResult.globalWithdrawalCommitted, true)
  assert.equal(withdrawalResult.revocationCleared, true)
  assert.notEqual(confirmationResult.status, "confirmed")
  assert.equal(permissionReads, 1) // host-state verification only; no confirmation provider read
  assert.equal(hostPermission, false)
  assert.equal((await adapter.trackCommittedTransition("must_remain_blocked", {})).status, "consentWithdrawn")
})

test("host persistence failure keeps a shared gate closed for an already-granted peer", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let hostPermission = true
  let denialPending = false
  const generation = makeGeneration()
  const readCurrentAnalyticsPermission = () => denialPending ? false : hostPermission
  const isAnalyticsPermissionAllowed = () => !denialPending && hostPermission
  const persistAnalyticsPermissionDenied = () => {
    generation.advance()
    denialPending = true
    throw new Error("host store unavailable")
  }
  const aView = h.tab(), bView = h.tab()
  const a = await createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: aView.runtime },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  const b = await createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: bView.runtime },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  t.after(() => { a.dispose(); b.dispose() })

  assert.equal((await a.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await b.confirmCurrentPermission()).status, "confirmed")
  await b.trackCommittedTransition("queued_before_failed_host_write", {})

  let failure
  try { await bounded(a.withdrawConsent()) } catch (error) { failure = error }
  assert.match(failure?.message ?? "", /Could not persist and verify host analytics denial/)
  assert.equal(failure.withdrawalResult.globalWithdrawalCommitted, true)
  assert.equal(failure.withdrawalResult.revocationCleared, true)
  assert.equal(hostPermission, true)
  assert.equal(denialPending, true)
  assert.equal(isAnalyticsPermissionAllowed(), false)
  assert.equal((await b.trackCommittedTransition("must_not_enqueue", {})).status, "consentWithdrawn")
  assert.equal((await b.flush()).status, "paused")
  assert.equal(h.requests.length, 0)
})

test("adapter honors config.locks and detaches event data and timestamps before lock waits", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const hostLocks = new FakeWebLocks()
  const generation = makeGeneration()
  const adapter = await createGameAnalyticsAdapter({
    config: { ...h.cfg, locks: hostLocks },
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })
  t.after(() => adapter.dispose())

  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  const scope = `rawbbit-browser-game:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const callTimestamp = new Date(h.now).toISOString()
  const entered = deferred(), release = deferred()
  const blocker = hostLocks.request(`${scope}:host-consent`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise

  const singleParams = { score: 1 }
  let singleSettled = false, batchSettled = false
  const single = adapter.trackCommittedTransition("single_snapshot", singleParams).then(result => {
    singleSettled = true
    return result
  })
  singleParams.score = 99
  const batchParams = { score: 3 }
  const batch = [{ eventName: "batch_snapshot", eventProperties: batchParams }]
  const batchResult = adapter.trackCommittedTransitions(batch).then(result => {
    batchSettled = true
    return result
  })
  batchParams.score = 100
  batch[0].eventName = "mutated_after_call"
  batch.push({ eventName: "late_batch_item", eventProperties: {} })
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(singleSettled, false, "top-level config.locks must control adapter coordination")
  assert.equal(batchSettled, false)
  h.advance(5000)

  release.resolve()
  await blocker
  assert.equal((await bounded(single)).status, "durablyStored")
  assert.equal((await bounded(batchResult)).status, "durablyStored")
  assert.equal((await adapter.flush()).status, "empty")
  const events = h.requests[0].events
  assert.deepEqual(events.map(event => event.event_name), ["session_started", "single_snapshot", "batch_snapshot"])
  assert.equal(events[1].event_params.score, 1)
  assert.equal(events[2].event_params.score, 3)
  assert.equal(events[1].event_timestamp, callTimestamp)
  assert.equal(events[2].event_timestamp, callTimestamp)
})

test("adapter reservations bound queued track operations and aggregate snapshot bytes", async t => {
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)

  const operationHarness = makeHarness(t)
  let operationAllowed = true
  const operationGeneration = makeGeneration()
  const operationAdapter = await createGameAnalyticsAdapter({
    config: { ...operationHarness.cfg, maxPendingTrackOperations: 1, maxPendingTrackBytes: 4096 },
    readCurrentAnalyticsPermission: () => operationAllowed,
    readAnalyticsPermissionGeneration: operationGeneration.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => operationAllowed,
    persistAnalyticsPermissionDenied: async () => { operationGeneration.advance(); operationAllowed = false },
  })
  t.after(() => operationAdapter.dispose())
  assert.equal((await operationAdapter.confirmCurrentPermission()).status, "confirmed")
  const operationScope = `rawbbit-browser-game:${encodeURIComponent(operationHarness.cfg.appId)}:${encodeURIComponent(operationHarness.cfg.environment)}`
  const operationEntered = deferred(), releaseOperations = deferred()
  const operationBlocker = operationHarness.locks.request(`${operationScope}:host-consent`, async () => {
    operationEntered.resolve()
    await releaseOperations.promise
  })
  await operationEntered.promise
  const pendingOperation = operationAdapter.trackCommittedTransition("bounded_operation", {})
  assert.equal((await operationAdapter.trackCommittedTransition("over_operation_limit", {})).status, "busy")
  releaseOperations.resolve()
  await operationBlocker
  assert.equal((await bounded(pendingOperation)).status, "durablyStored")
  assert.equal((await operationAdapter.trackCommittedTransition("operation_slot_reused", {})).status, "durablyStored")

  const byteHarness = makeHarness(t)
  let byteAllowed = true
  const byteGeneration = makeGeneration()
  const byteAdapter = await createGameAnalyticsAdapter({
    config: { ...byteHarness.cfg, maxPendingTrackOperations: 10, maxPendingTrackBytes: 8192 },
    readCurrentAnalyticsPermission: () => byteAllowed,
    readAnalyticsPermissionGeneration: byteGeneration.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => byteAllowed,
    persistAnalyticsPermissionDenied: async () => { byteGeneration.advance(); byteAllowed = false },
  })
  t.after(() => byteAdapter.dispose())
  assert.equal((await byteAdapter.confirmCurrentPermission()).status, "confirmed")
  const byteScope = `rawbbit-browser-game:${encodeURIComponent(byteHarness.cfg.appId)}:${encodeURIComponent(byteHarness.cfg.environment)}`
  const byteEntered = deferred(), releaseBytes = deferred()
  const byteBlocker = byteHarness.locks.request(`${byteScope}:host-consent`, async () => {
    byteEntered.resolve()
    await releaseBytes.promise
  })
  await byteEntered.promise
  const payload = "x".repeat(4200)
  const pendingBytes = byteAdapter.trackCommittedTransition("bounded_bytes", { payload })
  assert.equal((await byteAdapter.trackCommittedTransition("over_byte_limit", { payload })).status, "busy")
  releaseBytes.resolve()
  await byteBlocker
  assert.equal((await bounded(pendingBytes)).status, "durablyStored")
  assert.equal((await byteAdapter.trackCommittedTransition("byte_slot_reused", { payload })).status, "durablyStored")
})

test("invalid event names cannot run caller code and snapshot traps cannot bypass reservations", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const generation = makeGeneration()
  const adapter = await createGameAnalyticsAdapter({
    config: { ...h.cfg, maxPendingTrackOperations: 1, maxPendingTrackBytes: 4096 },
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })
  t.after(() => adapter.dispose())
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")

  let toJSONCalls = 0
  let reentrantTrack
  const invalidName = await adapter.trackCommittedTransition({
    toJSON() {
      toJSONCalls += 1
      reentrantTrack = adapter.trackCommittedTransition("reentered_from_toJSON", {})
      return "not_a_string_event_name"
    },
  }, {})
  assert.equal(invalidName.status, "invalidEvent")
  assert.equal(toJSONCalls, 0)
  assert.equal(reentrantTrack, undefined)

  const originalStringify = JSON.stringify
  let serializedDuringOversizedName = false
  let oversizedNameCall
  try {
    JSON.stringify = (...args) => {
      serializedDuringOversizedName = true
      return originalStringify(...args)
    }
    oversizedNameCall = adapter.trackCommittedTransition("x".repeat(1_000_000), {})
  } finally {
    JSON.stringify = originalStringify
  }
  assert.equal((await oversizedNameCall).status, "invalidEvent")
  assert.equal(serializedDuringOversizedName, false, "oversized names must fail before JSON serialization")
  assert.equal((await adapter.getDiagnostics()).pendingCount, 0)

  const reentrantResults = []
  const properties = new Proxy({ score: 1 }, {
    ownKeys(target) {
      reentrantResults.push(adapter.trackCommittedTransition("reentered_from_proxy", {}))
      return Reflect.ownKeys(target)
    },
  })
  assert.equal((await adapter.trackCommittedTransition("outer_reserved_first", properties)).status, "durablyStored")
  assert.ok(reentrantResults.length > 0)
  for (const result of reentrantResults) assert.equal((await result).status, "busy")
  assert.equal((await adapter.flush()).status, "empty")
  assert.deepEqual(h.requests[0].events.map(event => event.event_name), ["session_started", "outer_reserved_first"])
})

test("tracking reentered from adapter-owned SDK hooks is rejected synchronously", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const generation = makeGeneration()
  let adapter
  const reentrantResults = new Map()
  const requestReentrantTrack = hookName => {
    if (reentrantResults.has(hookName)) return
    reentrantResults.set(hookName, adapter.trackCommittedTransition(`reentered_from_${hookName}`, {}))
  }
  adapter = await createGameAnalyticsAdapter({
    config: h.cfg,
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
    sessionStartFactory: () => {
      requestReentrantTrack("session_start_factory")
      return null
    },
    eventContextMapper: () => {
      requestReentrantTrack("event_context_mapper")
      return null
    },
  })
  t.after(() => adapter.dispose())

  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await adapter.trackCommittedTransition("outer_hook_event", {})).status, "durablyStored")
  assert.equal(reentrantResults.size, 2)
  for (const result of reentrantResults.values()) {
    assert.deepEqual(await bounded(result), { status: "invalidEvent", safeDiagnosticCode: "callbackReentrancy" })
  }
  assert.equal((await adapter.getDiagnostics()).pendingCount, 1)
  await adapter.flush()
  assert.deepEqual(h.requests[0].events.map(event => event.event_name), ["outer_hook_event"])
})

test("adapter initialization prunes retained events with the configured clock", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  const configuredTime = Date.now() - 31 * 24 * 60 * 60 * 1000
  const config = { ...h.cfg, clock: () => configuredTime }
  let allowed = true
  const generation = makeGeneration()
  const createAdapter = () => createGameAnalyticsAdapter({
    config,
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })

  const first = await createAdapter()
  assert.equal((await first.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await first.trackCommittedTransition("retained_under_configured_clock", {})).status, "durablyStored")
  assert.equal((await first.getDiagnostics()).pendingCount, 2)
  first.dispose()

  const reopened = await createAdapter()
  t.after(() => reopened.dispose())
  assert.equal((await reopened.getDiagnostics()).pendingCount, 2)
  assert.equal((await reopened.getDiagnostics()).counters.expired, 0)
})

test("withdrawal drains an admission already inside the host lock before purging it", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const generation = makeGeneration()
  const adapter = await createGameAnalyticsAdapter({
    config: h.cfg,
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })
  t.after(() => adapter.dispose())
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")

  const scope = `rawbbit-browser-game:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const stateEntered = deferred(), releaseState = deferred()
  const stateBlocker = h.locks.request(`${scope}:state`, async () => {
    stateEntered.resolve()
    await releaseState.promise
  })
  await stateEntered.promise

  const track = adapter.trackCommittedTransition("admitted_before_denial", {})
  const hostLock = `${scope}:host-consent`
  const stateEntry = h.locks.entries.get(`${scope}:state`)
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (h.locks.held(hostLock) && stateEntry.queue.length > 0) break
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(h.locks.held(hostLock), true, "track must hold the host lock while SDK admission waits")
  assert.ok(stateEntry.queue.length > 0)

  const withdrawal = adapter.withdrawConsent()
  assert.equal(allowed, false)
  releaseState.resolve()
  await stateBlocker
  const [trackResult, withdrawalResult] = await Promise.all([
    bounded(track),
    bounded(withdrawal),
  ])
  assert.equal(trackResult.status, "durablyStored")
  assert.equal(withdrawalResult.globalWithdrawalCommitted, true)
  assert.equal(withdrawalResult.revocationCleared, true)
  assert.equal((await adapter.getDiagnostics()).pendingCount, 0)
  assert.equal(h.requests.length, 0)
})

test("flush timeout includes and cancels the initial host-lock wait", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const generation = makeGeneration()
  const adapter = await createGameAnalyticsAdapter({
    config: { ...h.cfg, lockTimeoutMs: 500 },
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })
  t.after(() => adapter.dispose())
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  await adapter.trackCommittedTransition("must_remain_queued", {})

  const scope = `rawbbit-browser-game:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const entered = deferred(), release = deferred()
  const blocker = h.locks.request(`${scope}:host-consent`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise
  const flushing = adapter.flush({ timeoutMs: 35 })
  const entry = h.locks.entries.get(`${scope}:host-consent`)
  for (let attempt = 0; attempt < 100 && entry.queue.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.ok(entry.queue.length > 0, "flush must wait for its initial host permission check")
  assert.equal((await bounded(flushing, 500)).status, "timeout")
  assert.equal(entry.queue.length, 0, "flush deadline must cancel the initial queued lock request")

  release.resolve()
  await blocker
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal((await adapter.getDiagnostics()).pendingCount, 2)
  assert.equal(h.requests.length, 0)
})

test("flush forwards only the remaining deadline after acquiring the host lock", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const generation = makeGeneration()
  const adapter = await createGameAnalyticsAdapter({
    config: { ...h.cfg, lockTimeoutMs: 500 },
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })
  t.after(() => adapter.dispose())
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  await adapter.trackCommittedTransition("remaining_flush_budget", {})

  h.setFetch((_url, init) => new Promise(resolve => {
    init.signal.addEventListener("abort", () => {
      resolve(collectorResponse(503, { accepted_events: 0 }))
    }, { once: true })
  }))
  const scope = `rawbbit-browser-game:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const entered = deferred(), release = deferred()
  const blocker = h.locks.request(`${scope}:host-consent`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise
  const startedAt = Date.now()
  const flushing = adapter.flush({ timeoutMs: 120 })
  const entry = h.locks.entries.get(`${scope}:host-consent`)
  for (let attempt = 0; attempt < 100 && entry.queue.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.ok(entry.queue.length > 0)
  await new Promise(resolve => setTimeout(resolve, 60))
  release.resolve()
  await blocker
  assert.equal((await bounded(flushing, 300)).status, "timeout")
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(h.requests.length, 1, "the flush should have started its single Fetch before deadline")
  assert.equal(h.requests[0].init.signal.aborted, true, "Fetch must be aborted within the original total deadline")
  assert.ok(Date.now() - startedAt < 170,
    "the SDK flush must receive only the remaining time, not a fresh full timeout")
})

test("an aborted flush cancels its queued host-lock Fetch invocation", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const generation = makeGeneration()
  const adapter = await createGameAnalyticsAdapter({
    config: { ...h.cfg, lockTimeoutMs: 500 },
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })
  t.after(() => adapter.dispose())

  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  await adapter.trackCommittedTransition("pending_for_flush", {})
  const scope = `rawbbit-browser-game:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const entered = deferred(), release = deferred()
  const flushing = adapter.flush({ timeoutMs: 35 })
  const blocker = h.locks.request(`${scope}:host-consent`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise

  const entry = h.locks.entries.get(`${scope}:host-consent`)
  for (let attempt = 0; attempt < 100 && entry.queue.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.ok(entry.queue.length > 0, "sender must be waiting for the host permission lock")
  assert.equal((await bounded(flushing, 500)).status, "timeout")
  for (let attempt = 0; attempt < 100 && entry.queue.length > 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(entry.queue.length, 0, "abort must cancel the queued host-lock request")

  release.resolve()
  await blocker
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(h.requests.length, 0)
})

test("an old batch queued at Fetch start cannot escape withdrawal after immediate re-consent", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let allowed = true
  const generation = makeGeneration()
  const adapter = await createGameAnalyticsAdapter({
    config: { ...h.cfg, lockTimeoutMs: 500 },
    readCurrentAnalyticsPermission: () => allowed,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed: () => allowed,
    persistAnalyticsPermissionDenied: async () => { generation.advance(); allowed = false },
  })
  t.after(() => adapter.dispose())

  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  await adapter.trackCommittedTransition("must_be_purged", {})
  const scope = `rawbbit-browser-game:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const lockName = `${scope}:host-consent`
  const entered = deferred(), release = deferred()
  const flushing = adapter.flush({ timeoutMs: 1500 })
  const blocker = h.locks.request(lockName, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise

  const entry = h.locks.entries.get(lockName)
  for (let attempt = 0; attempt < 100 && entry.queue.length === 0; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.ok(entry.queue.length > 0, "old sender Fetch must be queued behind the host lock")

  const withdrawal = adapter.withdrawConsent()
  assert.equal(allowed, false)
  release.resolve()
  await blocker
  const [flushResult, withdrawalResult] = await Promise.all([
    bounded(flushing, 2000),
    bounded(withdrawal, 2000),
  ])
  assert.notEqual(flushResult.status, "sent")
  assert.equal(withdrawalResult.globalWithdrawalCommitted, true)
  assert.equal(withdrawalResult.revocationCleared, true)
  assert.equal(h.requests.length, 0)

  allowed = true
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  await adapter.trackCommittedTransition("fresh_after_reconsent", {})
  await adapter.flush()
  assert.equal(h.requests.length, 1)
  assert.deepEqual(h.requests[0].events.map(event => event.event_name), ["session_started", "fresh_after_reconsent"])
})

test("permission generation fences an admission whose host-lock registration is delayed", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  const locks = delayNthHostLockRequest(h.locks)
  const generation = makeGeneration()
  let allowed = true
  const readCurrentAnalyticsPermission = () => allowed
  const isAnalyticsPermissionAllowed = () => allowed
  const persistAnalyticsPermissionDenied = async () => {
    generation.advance()
    allowed = false
  }
  const createAdapter = view => createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: view.runtime, locks, lockTimeoutMs: 1000 },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  const adapter = await createAdapter(h.tab())
  const peer = await createAdapter(h.tab())
  t.after(() => { adapter.dispose(); peer.dispose() })
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await peer.confirmCurrentPermission()).status, "confirmed")

  const registrationReady = locks.delayNextHostRequest()
  const delayedTrack = peer.trackCommittedTransition("must_not_admit_after_purge", {})
  await bounded(registrationReady)
  const withdrawal = await bounded(adapter.withdrawConsent())
  assert.equal(withdrawal.globalWithdrawalCommitted, true)
  assert.equal(withdrawal.revocationCleared, true)
  allowed = true
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")

  locks.releaseDelayedRequest()
  assert.equal((await bounded(delayedTrack)).status, "consentWithdrawn")
  assert.equal((await peer.getDiagnostics()).pendingCount, 0)
  assert.equal(h.requests.length, 0)

  assert.equal((await peer.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await peer.trackCommittedTransition("fresh_after_admission_fence", {})).status, "durablyStored")
  await peer.flush()
  assert.equal(h.requests.length, 1)
  assert.deepEqual(h.requests[0].events.map(event => event.event_name), [
    "session_started",
    "fresh_after_admission_fence",
  ])
})

test("a Fetch whose host-lock registration is delayed past withdrawal is fenced after re-consent", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  const locks = delayNthHostLockRequest(h.locks)
  const generation = makeGeneration()
  let allowed = true
  let denialPending = false
  const readCurrentAnalyticsPermission = () => denialPending ? false : allowed
  const isAnalyticsPermissionAllowed = () => !denialPending && allowed
  const persistAnalyticsPermissionDenied = async () => {
    // The shared generation changes in the synchronous deny prefix so a
    // previously captured Fetch cannot regain permission after later opt-in.
    generation.advance()
    denialPending = true
    allowed = false
    denialPending = false
  }
  const createAdapter = view => createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: view.runtime, locks, lockTimeoutMs: 1000 },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  const adapter = await createAdapter(h.tab())
  const peer = await createAdapter(h.tab())
  t.after(() => { adapter.dispose(); peer.dispose() })

  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await peer.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await peer.trackCommittedTransition("must_not_escape_purge", {})).status, "durablyStored")

  const delayedReady = locks.delayFetchRequest()
  const oldFlush = peer.flush({ timeoutMs: 3000 })
  await bounded(delayedReady)
  assert.equal(h.requests.length, 0, "the delayed lock registration must not have invoked Fetch")

  const withdrawal = await bounded(adapter.withdrawConsent())
  assert.equal(withdrawal.globalWithdrawalCommitted, true)
  assert.equal(withdrawal.revocationCleared, true)
  allowed = true
  assert.equal((await adapter.confirmCurrentPermission()).status, "confirmed")

  locks.releaseDelayedRequest()
  assert.notEqual((await bounded(oldFlush)).status, "sent")
  assert.equal(h.requests.length, 0, "the old batch must not be sent after re-consent")
  assert.equal((await peer.getDiagnostics()).pendingCount, 0)

  assert.equal((await peer.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await peer.trackCommittedTransition("fresh_after_fenced_fetch", {})).status, "durablyStored")
  await peer.flush()
  assert.equal(h.requests.length, 1)
  assert.deepEqual(h.requests[0].events.map(event => event.event_name), [
    "session_started",
    "fresh_after_fenced_fetch",
  ])
})

test("shared host denial gate fences a second adapter while persistence is pending", async t => {
  const h = makeHarness(t)
  const { createGameAnalyticsAdapter } = await loadAdapterTemplate(t)
  let hostPermission = true
  let denialPending = false
  const generation = makeGeneration()
  const persistEntered = deferred(), persistRelease = deferred()
  const readCurrentAnalyticsPermission = () => denialPending ? false : hostPermission
  const isAnalyticsPermissionAllowed = () => !denialPending && hostPermission
  const persistAnalyticsPermissionDenied = () => {
    // Close the shared gate synchronously so all adapters read denied before
    // the asynchronous durable write begins.
    generation.advance()
    denialPending = true
    persistEntered.resolve()
    return persistRelease.promise.then(() => {
      hostPermission = false
      denialPending = false
    })
  }
  const aView = h.tab(), bView = h.tab()
  const a = await createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: aView.runtime },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  const b = await createGameAnalyticsAdapter({
    config: { ...h.cfg, runtime: bView.runtime },
    readCurrentAnalyticsPermission,
    readAnalyticsPermissionGeneration: generation.readAnalyticsPermissionGeneration,
    isAnalyticsPermissionAllowed,
    persistAnalyticsPermissionDenied,
  })
  t.after(() => { a.dispose(); b.dispose() })

  assert.equal((await a.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await b.confirmCurrentPermission()).status, "confirmed")
  await a.trackCommittedTransition("before_shared_withdrawal", {})

  const withdrawal = a.withdrawConsent()
  assert.equal(readCurrentAnalyticsPermission(), false)
  await persistEntered.promise

  // Wait until the SDK has purged and cleared its marker, while host persistence
  // is deliberately still pending. The peer must consult the shared deny gate.
  let records = 1
  for (let attempt = 0; attempt < 100; attempt += 1) {
    records = (await a.getDiagnostics()).pendingCount
    if (records === 0 && h.pendingRevocation === null) break
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.equal(records, 0)
  assert.equal(h.pendingRevocation, null)

  const peerConfirmation = await bounded(b.confirmCurrentPermission())
  const peerTrack = await bounded(b.trackCommittedTransition("must_not_enqueue", {}))
  assert.equal(peerConfirmation.status, "consentWithdrawn")
  assert.equal(peerTrack.status, "consentWithdrawn")
  persistRelease.resolve()
  const completed = await bounded(withdrawal)
  assert.equal(completed.globalWithdrawalCommitted, true)
  assert.equal(completed.revocationCleared, true)
  assert.equal(hostPermission, false)
  assert.equal((await b.getDiagnostics()).pendingCount, 0)
  assert.equal(h.requests.length, 0)

  // Explicit opt-in after purge completion must not be trapped behind a stale
  // in-flight withdrawal promise, and only post-reconsent events are delivered.
  hostPermission = true
  assert.equal((await a.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await b.confirmCurrentPermission()).status, "confirmed")
  assert.equal((await a.trackCommittedTransition("after_reconsent", {})).status, "durablyStored")
  assert.equal((await a.getDiagnostics()).pendingCount, 2)
  const delivery = await a.flush()
  assert.equal(delivery.status, "empty")
  assert.equal(h.requests.length, 1)
  assert.deepEqual(h.requests[0].events.map(event => event.event_name), ["session_started", "after_reconsent"])
})
