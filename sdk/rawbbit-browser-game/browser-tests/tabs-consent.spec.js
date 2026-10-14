import { randomUUID } from "node:crypto"
import { test, expect } from "@playwright/test"
import { APP_ORIGIN, configure, endpoint, installClient, release, snapshot } from "./server.mjs"

const scope = () => `test-${randomUUID()}`
const grant = page => page.evaluate(() => c.confirmConsent(() => true))
const track = (page, eventName) => page.evaluate(eventName => c.track({ eventName }), eventName)
const flush = page => page.evaluate(() => c.flush())
const beginFlush = page => page.evaluate(() => { window.flushing = c.flush(); return true })
const stateGate = appId => `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test:state`
const holdStateGate = async (page, appId) => {
  await page.evaluate(name => {
    window.gateHeld = false
    window.gateWork = navigator.locks.request(name, () => new Promise(resolve => {
      window.gateHeld = true
      window.releaseGate = resolve
    }))
  }, stateGate(appId))
  await expect.poll(() => page.evaluate(() => window.gateHeld)).toBe(true)
}
const pendingStateGates = (page, appId) => page.evaluate(async name =>
  (await navigator.locks.query()).pending.filter(lock => lock.name === name).length, stateGate(appId))
const releaseStateGate = async page => {
  await page.evaluate(() => window.releaseGate())
  await page.evaluate(() => window.gateWork)
}

test("two same-origin tabs share installation/session and commit the session prefix exactly once", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  const peer = await context.newPage()
  const ready = await Promise.all([page, peer].map(tab => installClient(tab, testInfo, { appId, prefix: true })))
  if (ready.some(value => !value)) return
  await Promise.all([grant(page), grant(peer)])
  const stored = await Promise.all([track(page, "tab_one"), track(peer, "tab_two")])
  expect(stored.map(result => result.status)).toEqual(["durablyStored", "durablyStored"])
  await page.bringToFront()
  await flush(page)
  const received = await snapshot(request, appId)
  const events = received.requests.flatMap(entry => entry.events)
  expect(events.length).toBe(3)
  expect(events.filter(event => event.event_name === "session_start").length).toBe(1)
  expect(new Set(events.map(event => event.user.user_pseudo_id)).size).toBe(1)
  expect(new Set(events.map(event => event.user.session_id)).size).toBe(1)
  expect(stored.every(result => events.some(event => event.event_id === result.eventIds[0]))).toBe(true)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
})

test("one ordinary sender owns both tabs' queue across a real delayed HTTP request", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { mode: "hold" })
  if (!await installClient(page, testInfo, { appId, timeoutMs: 5000 })) return
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, { appId, timeoutMs: 5000 })) return
  await Promise.all([grant(page), grant(peer)])
  await Promise.all([track(page, "sender_one"), track(peer, "sender_two")])
  await page.bringToFront()
  await beginFlush(page)
  await expect.poll(async () => (await snapshot(request, appId)).posts).toBe(1)
  expect((await flush(peer)).status).toBe("busy")
  expect((await snapshot(request, appId)).posts).toBe(1)
  await release(request, appId)
  await page.evaluate(() => window.flushing)
  expect((await peer.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
  expect((await snapshot(request, appId)).accepted).toBe(2)
})

test("withdrawal is enforced durably even when BroadcastChannel advisory messages are lost", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  const options = { appId, runtime: { BroadcastChannel: null } }
  if (!await installClient(page, testInfo, options)) return
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, options)) return
  await Promise.all([grant(page), grant(peer)])
  expect((await track(page, "purge_me")).status).toBe("durablyStored")
  const withdrawn = await peer.evaluate(() => c.withdrawConsent())
  expect(withdrawn.status).toBe("withdrawn")
  expect(withdrawn.globalWithdrawalCommitted).toBe(true)
  expect(withdrawn.removed).toBe(1)
  await page.bringToFront()
  expect((await flush(page)).status).toBe("consentUnknown")
  expect((await track(page, "stale_permission")).status).toBe("consentUnknown")
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
  expect((await snapshot(request, appId)).posts).toBe(0)
  await grant(page)
  expect((await track(page, "fresh_permission")).status).toBe("durablyStored")
  await flush(page)
  expect((await snapshot(request, appId)).posts).toBe(1)
})

test("a late real HTTP ACK from before withdrawal cannot delete newly consented data", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { mode: "hold" })
  const options = { appId, timeoutMs: 5000, runtime: { BroadcastChannel: null } }
  if (!await installClient(page, testInfo, options)) return
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, options)) return
  await Promise.all([grant(page), grant(peer)])
  const old = await track(page, "old_epoch")
  await page.bringToFront()
  await beginFlush(page)
  await expect.poll(async () => (await snapshot(request, appId)).posts).toBe(1)
  expect((await peer.evaluate(() => c.withdrawConsent())).globalWithdrawalCommitted).toBe(true)
  await grant(page)
  const fresh = await track(page, "new_epoch")
  expect(fresh.status).toBe("durablyStored")
  await release(request, appId)
  expect((await page.evaluate(() => window.flushing)).status).toBe("paused")
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(1)
  await configure(request, appId, { mode: "normal" })
  await flush(page)
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(2)
  expect(received.requests[0].events[0].event_id === old.eventIds[0]).toBe(true)
  expect(received.requests[1].events[0].event_id === fresh.eventIds[0]).toBe(true)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
})

test("permission confirmation captured before a peer withdrawal cannot grant stale policy", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  const options = { appId, runtime: { BroadcastChannel: null } }
  if (!await installClient(page, testInfo, options)) return
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, options)) return
  await grant(peer)
  await page.evaluate(() => {
    window.confirming = c.confirmConsent(() => new Promise(resolve => { window.answerPermission = resolve }))
  })
  await expect.poll(() => page.evaluate(() => typeof window.answerPermission)).toBe("function")
  expect((await peer.evaluate(() => c.withdrawConsent())).globalWithdrawalCommitted).toBe(true)
  await page.evaluate(() => window.answerPermission(true))
  expect((await page.evaluate(() => window.confirming)).status).toBe("consentUnknown")
  expect((await track(page, "stale_grant")).status).toBe("consentUnknown")
  await flush(page)
  expect((await snapshot(request, appId)).posts).toBe(0)
})

test("closing a sending tab releases ownership but preserves retry reservation and event IDs", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { mode: "hold" })
  if (!await installClient(page, testInfo, { appId, timeoutMs: 1000 })) return
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, { appId, timeoutMs: 1000 })) return
  await Promise.all([grant(page), grant(peer)])
  const stored = await track(page, "owner_closed")
  await page.bringToFront()
  await beginFlush(page)
  await expect.poll(async () => (await snapshot(request, appId)).posts).toBe(1)
  await page.close()
  await configure(request, appId, { mode: "normal" })
  await peer.bringToFront()
  await expect.poll(async () => {
    const result = await flush(peer)
    expect(["busy", "pending"].includes(result.status)).toBe(true)
    return result.status
  }).toBe("pending")
  expect((await snapshot(request, appId)).posts).toBe(1)
  await peer.evaluate(() => { window.now += 2000 })
  await flush(peer)
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(2)
  expect(received.requests.every(entry => entry.events[0].event_id === stored.eventIds[0])).toBe(true)
  expect((await peer.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
})

test("endpoint migration without broadcasts fences the old tab and sends retained data only to the new binding", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId, runtime: { BroadcastChannel: null } })) return
  await grant(page)
  const stored = await track(page, "migrate_tabs")
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, { appId, collectorUrl: endpoint(appId, "b"), previousCollectorUrl: endpoint(appId), runtime: { BroadcastChannel: null } })) return
  await page.bringToFront()
  expect((await flush(page)).status).toBe("consentUnknown")
  expect((await grant(page)).status).toBe("consentUnknown")
  expect((await snapshot(request, appId)).posts).toBe(0)
  await peer.bringToFront()
  await grant(peer)
  await flush(peer)
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(1)
  expect(received.requests[0].destination).toBe("b")
  expect(received.requests[0].events[0].event_id === stored.eventIds[0]).toBe(true)
})

test("withdrawal queued ahead of dispatch at a held real state gate prevents stale SDK fetch", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  const options = { appId, lockTimeoutMs: 5000, runtime: { BroadcastChannel: null } }
  if (!await installClient(page, testInfo, options)) return
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, options)) return
  await Promise.all([grant(page), grant(peer)])
  await track(page, "queued_before_barrier")
  await holdStateGate(peer, appId)
  await peer.evaluate(() => { window.withdrawing = c.withdrawConsent() })
  await expect.poll(() => pendingStateGates(peer, appId)).toBe(1)
  await beginFlush(page)
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(0)
  await releaseStateGate(peer)
  const withdrawal = await peer.evaluate(() => window.withdrawing)
  expect(withdrawal.globalWithdrawalCommitted).toBe(true)
  expect(withdrawal.revocationCleared).toBe(true)
  // WebKit can suspend the background page after opening `peer`; either the
  // withdrawal fence or that lifecycle pause is a valid no-send outcome.
  expect(["consentUnknown", "paused"]).toContain((await page.evaluate(() => window.flushing)).status)
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(0)
  expect((await snapshot(request, appId)).posts).toBe(0)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
})

test("failed withdrawal stays locally disabled and blocks reconfirmation until withdrawal is repeated", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId, lockTimeoutMs: 200, runtime: { BroadcastChannel: null } })) return
  await grant(page)
  const old = await track(page, "incomplete_withdrawal")
  const gate = await context.newPage()
  await gate.goto(APP_ORIGIN)
  await holdStateGate(gate, appId)
  const failed = await page.evaluate(() => c.withdrawConsent())
  expect(failed.status).toBe("busy")
  expect(failed.globalWithdrawalCommitted).toBe(false)
  expect(failed.localDisabled).toBe(true)
  const confirmation = await grant(page)
  expect(confirmation.status).toBe("consentUnknown")
  expect(confirmation.safeDiagnosticCode).toBe("withdrawalIncomplete")
  expect((await flush(page)).status).toBe("paused")
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(0)
  await releaseStateGate(gate)
  const incomplete = await page.evaluate(() => c.getDiagnostics())
  expect(incomplete.withdrawalIncomplete).toBe(true)
  expect(incomplete.pendingCount).toBe(1)
  expect((await grant(page)).safeDiagnosticCode).toBe("withdrawalIncomplete")
  const repeated = await page.evaluate(() => c.withdrawConsent())
  expect(repeated.globalWithdrawalCommitted).toBe(true)
  expect(repeated.removed).toBe(1)
  expect((await grant(page)).status).toBe("confirmed")
  const fresh = await track(page, "after_complete_withdrawal")
  await flush(page)
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(1)
  expect(received.requests[0].events[0].event_id === fresh.eventIds[0]).toBe(true)
  expect(received.requests.every(entry => entry.events.every(event => event.event_id !== old.eventIds[0]))).toBe(true)
})

test("endpoint migration reports busy while a real sender owns the old binding", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { mode: "hold" })
  if (!await installClient(page, testInfo, { appId, timeoutMs: 5000, runtime: { BroadcastChannel: null } })) return
  await grant(page)
  await track(page, "old_binding_in_flight")
  await beginFlush(page)
  await expect.poll(async () => (await snapshot(request, appId)).posts).toBe(1)
  const peer = await context.newPage()
  await peer.goto(APP_ORIGIN)
  const migration = { appId, collectorUrl: endpoint(appId, "b"), previousCollectorUrl: endpoint(appId), runtime: { BroadcastChannel: null } }
  const code = await peer.evaluate(async options => {
    try { await window.install(options); return "unexpectedSuccess" }
    catch (error) { return error.code }
  }, migration)
  expect(code).toBe("busy")
  expect((await snapshot(request, appId)).posts).toBe(1)
  await release(request, appId)
  await page.evaluate(() => window.flushing)
  const queued = await track(page, "retained_for_new_binding")
  expect(queued.status).toBe("durablyStored")
  await configure(request, appId, { mode: "normal" })
  if (!await installClient(peer, testInfo, migration, { navigate: false })) return
  expect((await flush(peer)).status).toBe("paused")
  await grant(peer)
  await flush(peer)
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(2)
  expect(received.requests.map(entry => entry.destination)).toEqual(["a", "b"])
  expect(received.requests[1].events[0].event_id === queued.eventIds[0]).toBe(true)
})

test("queue changes during a held recovery probe fence its ACK and preserve the shared fault", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { acceptedCount: 0 })
  const options = { appId, timeoutMs: 5000, runtime: { BroadcastChannel: null } }
  if (!await installClient(page, testInfo, options)) return
  const peer = await context.newPage()
  if (!await installClient(peer, testInfo, options)) return
  await Promise.all([grant(page), grant(peer)])
  const old = await track(page, "probe_original")
  expect((await flush(page)).status).toBe("paused")
  const before = await page.evaluate(() => c.getDiagnostics())
  const recovery = { expectedFaultRevision: before.faultRevision, expectedQueueRevision: before.queueRevision,
    reasonCode: "configurationCorrected", policyVersion: "synthetic-test-v1" }
  await configure(request, appId, { acceptedCount: null, mode: "hold" })
  await page.evaluate(options => { window.probing = c.validateAndResume(options) }, recovery)
  await expect.poll(async () => (await snapshot(request, appId)).posts).toBe(2)
  const fresh = await track(peer, "probe_queue_changed")
  expect(fresh.status).toBe("durablyStored")
  expect((await peer.evaluate(options => c.validateAndResume(options), recovery)).status).toBe("busy")
  expect(await peer.evaluate(() => sdkFetchInvocations)).toBe(0)
  await release(request, appId)
  const stale = await page.evaluate(() => window.probing)
  expect(stale.status).toBe("recoveryPending")
  expect(stale.safeDiagnosticCode).toBe("staleRecovery")
  const retained = await page.evaluate(() => c.getDiagnostics())
  expect(retained.pendingCount).toBe(2)
  expect(retained.faultCode).toBe("acceptedCountMismatch")
  expect(retained.faultRevision).toBe(before.faultRevision)
  await configure(request, appId, { mode: "normal" })
  await page.evaluate(() => { window.now += 6000 })
  const current = { ...recovery, expectedFaultRevision: retained.faultRevision, expectedQueueRevision: retained.queueRevision }
  expect((await page.evaluate(options => c.validateAndResume(options), current)).status).toBe("resumed")
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(1)
  await flush(page)
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(4)
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(4)
  expect(received.requests.slice(0, 3).every(entry => entry.events[0].event_id === old.eventIds[0])).toBe(true)
  expect(received.requests[3].events[0].event_id === fresh.eventIds[0]).toBe(true)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
})
