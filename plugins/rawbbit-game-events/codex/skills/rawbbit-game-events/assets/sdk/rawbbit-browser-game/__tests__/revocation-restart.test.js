import test from "node:test"
import assert from "node:assert/strict"
import { createLocalStorageRevocationStore } from "../index.js"
import { bounded, grant, makeHarness, readState, stored, deferred } from "./helpers.js"

test("failed queue purge stays durably blocking across dispose and client restart", async t => {
  const h = makeHarness(t)
  const oldClient = await h.create({ lockTimeoutMs: 15 })
  await grant(oldClient)
  const before = await stored(oldClient, { eventName: "must_not_be_replayed" })
  const installationId = (await readState(oldClient)).meta.installationId

  const release = deferred(), entered = deferred()
  const holder = h.locks.request(`${oldClient.scope}:state`, async () => { entered.resolve(); await release.promise })
  await entered.promise
  const failed = await oldClient.withdrawConsent()
  assert.equal(failed.globalWithdrawalCommitted, false)
  assert.equal(failed.localDisabled, true)
  assert.notEqual(h.pendingRevocation, null)
  release.resolve(); await holder
  oldClient.dispose()

  const restarted = await h.create({}, h.tab())
  let permissionReads = 0
  const blocked = await bounded(restarted.confirmConsent(() => { permissionReads += 1; return true }))
  assert.equal(blocked.status, "consentUnknown")
  assert.equal(blocked.safeDiagnosticCode, "withdrawalIncomplete")
  assert.equal(permissionReads, 0)
  assert.equal((await restarted.track({ eventName: "not_allowed" })).status, "consentUnknown")
  assert.equal((await restarted.flush()).safeDiagnosticCode, "consentOrLifecyclePaused")
  assert.equal(h.requests.length, 0)
  assert.equal((await readState(restarted)).records.length, 1)
  assert.equal((await readState(restarted)).meta.installationId, installationId)

  const repaired = await bounded(restarted.withdrawConsent())
  assert.equal(repaired.globalWithdrawalCommitted, true)
  assert.equal(repaired.revocationCleared, true)
  assert.equal(h.pendingRevocation, null)
  assert.equal((await readState(restarted)).records.length, 0)

  await grant(restarted)
  const after = await stored(restarted, { eventName: "after_repair" })
  assert.equal((await restarted.flush()).status, "empty")
  assert.equal(h.requests.length, 1)
  assert.deepEqual(h.requests.flatMap(request => request.events.map(event => event.event_id)), after.eventIds)
  assert.equal(h.requests.flatMap(request => request.events).some(event => event.event_id === before.eventIds[0]), false)
})

test("journal clear failure after committed purge remains blocked across restart", async t => {
  const h = makeHarness(t)
  const client = await h.create()
  await grant(client); await stored(client)
  const clear = h.revocationStore.clearPending
  h.revocationStore.clearPending = async () => { throw new Error("synthetic unavailable journal") }
  const result = await client.withdrawConsent()
  assert.equal(result.globalWithdrawalCommitted, true)
  assert.equal(result.revocationCleared, false)
  assert.equal(result.localDisabled, true)
  assert.notEqual(h.pendingRevocation, null)
  assert.equal((await client.getDiagnostics()).revocationStorageCode, "revocationStorageFailure")

  h.revocationStore.clearPending = clear
  client.dispose()
  const restarted = await h.create({}, h.tab())
  assert.equal((await restarted.confirmConsent(() => true)).safeDiagnosticCode, "withdrawalIncomplete")
  const repaired = await restarted.withdrawConsent()
  assert.equal(repaired.globalWithdrawalCommitted, true)
  assert.equal(repaired.revocationCleared, true)
  await grant(restarted)
})

test("journal read and marker-write failures fail closed without pretending the purge committed", async t => {
  const h = makeHarness(t)
  const client = await h.create()
  await grant(client); await stored(client)
  const mark = h.revocationStore.markPending
  h.revocationStore.markPending = async () => { throw new Error("synthetic marker write failure") }
  const failed = await client.withdrawConsent()
  assert.equal(failed.globalWithdrawalCommitted, false)
  assert.equal(failed.revocationCleared, false)
  assert.equal(failed.localDisabled, true)
  assert.equal(failed.safeDiagnosticCode, "revocationStorageFailure")
  assert.equal((await client.confirmConsent(() => true)).safeDiagnosticCode, "withdrawalIncomplete")
  assert.equal((await readState(client)).records.length, 1)
  assert.equal(h.requests.length, 0)

  h.revocationStore.markPending = mark
  const read = h.revocationStore.readPending
  h.revocationStore.readPending = async () => { throw new Error("synthetic marker read failure") }
  const readFailure = await client.confirmConsent(() => true)
  assert.equal(readFailure.status, "consentUnknown")
  assert.equal(readFailure.safeDiagnosticCode, "revocationStorageFailure")
  h.revocationStore.readPending = read
  const repaired = await client.withdrawConsent()
  assert.equal(repaired.globalWithdrawalCommitted, true)
  assert.equal(repaired.revocationCleared, true)
})

test("a peer's queued confirmation cannot run until the revocation purge and marker clear finish", async t => {
  const h = makeHarness(t)
  const a = await h.create({ lockTimeoutMs: 1000 })
  const b = await h.create({ lockTimeoutMs: 1000 }, h.tab())
  await grant(a); await stored(a, { eventName: "peer_revocation" })

  const release = deferred(), entered = deferred()
  const holder = h.locks.request(`${a.scope}:state`, async () => { entered.resolve(); await release.promise })
  await entered.promise
  const withdrawal = b.withdrawConsent()
  for (let attempt = 0; h.pendingRevocation === null && attempt < 100; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1))
  }
  assert.notEqual(h.pendingRevocation, null)
  let permissionReads = 0
  const confirmation = a.confirmConsent(() => { permissionReads += 1; return true })
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(permissionReads, 0)

  release.resolve(); await holder
  assert.equal((await withdrawal).revocationCleared, true)
  assert.equal((await confirmation).status, "confirmed")
  assert.equal(permissionReads, 1)
  assert.equal(a.hasCurrentConsent, true)
  assert.equal((await readState(a)).records.length, 0)
})

test("a pending peer revocation prevents further admissions while a purge is incomplete", async t => {
  const h = makeHarness(t)
  const oldTab = await h.create()
  const withdrawingTab = await h.create({ lockTimeoutMs: 15 }, h.tab())
  await grant(oldTab)
  await stored(oldTab, { eventName: "pre_withdrawal" })

  const entered = deferred(), release = deferred()
  const holder = h.locks.request(`${oldTab.scope}:state`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise
  const failed = await withdrawingTab.withdrawConsent()
  assert.equal(failed.globalWithdrawalCommitted, false)
  assert.notEqual(h.pendingRevocation, null)
  release.resolve(); await holder

  const blocked = await oldTab.track({ eventName: "must_not_enqueue_after_revoke_marker" })
  assert.equal(blocked.status, "consentUnknown")
  assert.equal(blocked.safeDiagnosticCode, "withdrawalIncomplete")
  assert.equal(oldTab.hasCurrentConsent, false)
  assert.equal((await readState(oldTab)).records.length, 1)
  assert.equal(h.requests.length, 0)

  const repaired = await withdrawingTab.withdrawConsent()
  assert.equal(repaired.globalWithdrawalCommitted, true)
  assert.equal(repaired.revocationCleared, true)
  assert.equal((await readState(oldTab)).records.length, 0)
})

test("a concurrent permission confirmation cannot cancel an explicit withdrawal", async t => {
  const h = makeHarness(t)
  const client = await h.create()
  await grant(client)
  const old = await stored(client, { eventName: "must_be_purged" })
  const mark = h.revocationStore.markPending
  const entered = deferred(), release = deferred()
  h.revocationStore.markPending = async () => {
    entered.resolve()
    await release.promise
    return mark()
  }

  const withdrawal = client.withdrawConsent()
  await entered.promise
  let permissionReads = 0
  const confirmation = client.confirmConsent(() => { permissionReads += 1; return true })
  release.resolve()
  const [withdrawalResult, confirmationResult] = await Promise.all([bounded(withdrawal), bounded(confirmation)])
  assert.equal(withdrawalResult.globalWithdrawalCommitted, true)
  assert.equal(withdrawalResult.revocationCleared, true)
  assert.equal(permissionReads, confirmationResult.status === "confirmed" ? 1 : 0)
  assert.ok(["confirmed", "consentUnknown"].includes(confirmationResult.status))
  assert.equal((await readState(client)).records.length, 0)
  assert.equal(h.pendingRevocation, null)
  assert.equal(h.requests.length, 0)
  assert.equal(old.eventIds.length, 1)
})

test("sender journal errors after a grant invalidate local consent", async t => {
  for (const mode of ["throws", "malformed"]) {
    await t.test(mode, async t => {
      const h = makeHarness(t)
      const client = await h.create()
      await grant(client)
      await stored(client)
      h.revocationStore.readPending = async () => {
        if (mode === "throws") throw new Error("synthetic private journal error")
        return "malformed marker"
      }

      const result = await client.flush()
      assert.equal(result.status, "consentUnknown")
      assert.equal(result.safeDiagnosticCode, mode === "throws" ? "revocationStorageFailure" : "revocationStorageInvalid")
      assert.equal(client.hasCurrentConsent, false)
      assert.equal((await client.track({ eventName: "must_not_admit" })).status, "consentUnknown")
      assert.equal(h.requests.length, 0)
    })
  }
})

test("endpoint migration before an obsolete denial keeps the queue without writing a marker", async t => {
  const h = makeHarness(t)
  const oldClient = await h.create()
  await grant(oldClient)
  const queued = await stored(oldClient, { eventName: "preserved_by_migration" })
  const decision = deferred(), checked = deferred()
  const staleDenial = oldClient.confirmConsent(() => { checked.resolve(); return decision.promise })
  await checked.promise
  const migrated = await h.create({
    collectorUrl: "https://collector-next.example",
    previousCollectorUrl: "https://collector.example",
  }, h.tab())
  decision.resolve(false)
  const staleResult = await bounded(staleDenial)
  assert.equal(staleResult.safeDiagnosticCode, "consentChanged")
  assert.equal(staleResult.globalWithdrawalCommitted, false)
  assert.equal(staleResult.revocationCleared, false)
  assert.equal(h.pendingRevocation, null)
  assert.equal((await readState(migrated)).records.length, 1)

  await grant(migrated)
  await migrated.flush()
  assert.equal(h.requests.length, 1)
  assert.equal(h.requests[0].url, "https://collector-next.example/v1/events:batch")
  assert.deepEqual(h.requests[0].events.map(event => event.event_id), queued.eventIds)
})

test("revocation-lock timeout keeps an explicit withdrawal blocking until repaired", async t => {
  const h = makeHarness(t)
  const client = await h.create({ lockTimeoutMs: 10 })
  await grant(client)
  const old = await stored(client, { eventName: "must_not_survive_lock_timeout" })

  const entered = deferred(), release = deferred()
  const holder = h.locks.request(`${client.scope}:revocation`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise
  const failed = await client.withdrawConsent()
  assert.equal(failed.status, "busy")
  assert.equal(failed.globalWithdrawalCommitted, false)
  assert.equal(failed.revocationCleared, false)
  release.resolve(); await holder

  let permissionReads = 0
  const blocked = await client.confirmConsent(() => { permissionReads += 1; return true })
  assert.equal(blocked.safeDiagnosticCode, "withdrawalIncomplete")
  assert.equal(permissionReads, 0)
  assert.equal((await client.track({ eventName: "must_not_admit" })).status, "consentUnknown")
  assert.equal((await readState(client)).records.length, 1)
  assert.equal(h.requests.length, 0)

  const repaired = await client.withdrawConsent()
  assert.equal(repaired.globalWithdrawalCommitted, true)
  assert.equal(repaired.revocationCleared, true)
  assert.equal((await readState(client)).records.length, 0)
  await grant(client)
  const fresh = await stored(client, { eventName: "only_after_repair" })
  assert.equal((await client.flush()).status, "empty")
  assert.deepEqual(h.requests.flatMap(request => request.events.map(event => event.event_id)), fresh.eventIds)
  assert.equal(h.requests.flatMap(request => request.events).some(event => event.event_id === old.eventIds[0]), false)
})

test("an older clear result cannot poison a newer successful withdrawal", async t => {
  const h = makeHarness(t)
  const client = await h.create({ lockTimeoutMs: 1000 })
  await grant(client); await stored(client, { eventName: "overlapping_withdrawals" })
  const clear = h.revocationStore.clearPending
  const entered = deferred(), release = deferred()
  let firstClear = true
  h.revocationStore.clearPending = async token => {
    if (firstClear) {
      firstClear = false
      entered.resolve()
      await release.promise
    }
    return clear(token)
  }

  const older = client.withdrawConsent()
  await entered.promise
  const newer = client.withdrawConsent()
  await new Promise(resolve => setTimeout(resolve, 5))
  release.resolve()
  const [olderResult, newerResult] = await Promise.all([bounded(older), bounded(newer)])
  assert.equal(olderResult.globalWithdrawalCommitted, true)
  assert.equal(newerResult.globalWithdrawalCommitted, true)
  assert.equal(newerResult.revocationCleared, true)
  assert.equal(newerResult.localDisabled, false)
  assert.equal(client._withdrawIncomplete, false)
  assert.equal(client._revocationUncertain, false)
  assert.equal(h.pendingRevocation, null)
  await grant(client)
})

test("explicit withdrawal replaces a malformed marker and repairs the queue", async t => {
  const h = makeHarness(t)
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.has(key) ? this.values.get(key) : null },
    setItem(key, value) { this.values.set(key, String(value)) },
    removeItem(key) { this.values.delete(key) },
  }
  const store = createLocalStorageRevocationStore({
    appId: h.cfg.appId, environment: h.cfg.environment, storage, crypto: globalThis.crypto,
  })
  const key = `rawbbit-browser-game:pending-revocation:${encodeURIComponent(h.cfg.appId)}:${encodeURIComponent(h.cfg.environment)}`
  const client = await h.create({ revocationStore: store })
  await grant(client)
  await stored(client, { eventName: "malformed_marker_repair" })

  storage.setItem(key, "corrupted marker")
  let permissionReads = 0
  const blocked = await client.confirmConsent(() => { permissionReads += 1; return true })
  assert.equal(blocked.status, "consentUnknown")
  assert.equal(blocked.safeDiagnosticCode, "revocationStorageFailure")
  assert.equal(permissionReads, 0)
  assert.equal((await readState(client)).records.length, 1)

  const repaired = await client.withdrawConsent()
  assert.equal(repaired.globalWithdrawalCommitted, true)
  assert.equal(repaired.revocationCleared, true)
  assert.equal(storage.getItem(key), null)
  assert.equal((await readState(client)).records.length, 0)
})

test("diagnostics do not expose lock contention as a journal error code", async t => {
  const h = makeHarness(t)
  const client = await h.create({ lockTimeoutMs: 10 })
  await grant(client)
  const entered = deferred(), release = deferred()
  const holder = h.locks.request(`${client.scope}:revocation`, async () => {
    entered.resolve()
    await release.promise
  })
  await entered.promise
  const diagnostics = await client.getDiagnostics()
  assert.equal(diagnostics.consent, "unknown")
  assert.equal(diagnostics.lastBusy, true)
  assert.equal(diagnostics.revocationStorageCode, null)
  release.resolve(); await holder
  await grant(client)
})
