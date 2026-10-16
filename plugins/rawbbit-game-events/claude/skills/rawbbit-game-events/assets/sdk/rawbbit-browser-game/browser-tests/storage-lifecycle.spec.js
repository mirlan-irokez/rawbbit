import { randomUUID } from "node:crypto"
import { test, expect } from "@playwright/test"
import { configure, endpoint, installClient, snapshot } from "./server.mjs"

// Playwright's page/context fixtures are fresh for every test: no reused origin storage.
const scope = () => `test-${randomUUID()}`
const databaseName = appId => `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test`

test("offline durable enqueue survives a real reload, but replay requires fresh permission", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId, autoFlush: true })) return
  expect((await page.evaluate(() => c.confirmConsent(() => true))).status).toBe("confirmed")
  await context.setOffline(true)
  await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(false)
  const stored = await page.evaluate(() => c.track({ eventName: "offline_action", eventParams: { checkpoint: 7 } }))
  expect(stored.status).toBe("durablyStored")
  expect((await page.evaluate(() => c.flush())).status).toBe("offline")
  expect((await snapshot(request, appId)).posts).toBe(0)

  // Dispose while offline: reconnecting the old granted page would legitimately pump.
  await page.evaluate(() => c.dispose())
  await context.setOffline(false)
  await page.reload()
  if (!await installClient(page, testInfo, { appId, autoFlush: true }, { navigate: false })) return
  const restored = await page.evaluate(() => c.getDiagnostics())
  expect(restored.consent).toBe("unknown")
  expect(restored.pendingCount).toBe(1)
  expect((await page.evaluate(() => c.track({ eventName: "not_permitted" }))).status).toBe("consentUnknown")
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(0)
  expect((await snapshot(request, appId)).posts).toBe(0)
  await context.setOffline(true)
  expect((await page.evaluate(() => c.confirmConsent(() => true))).status).toBe("confirmed")
  expect((await page.evaluate(() => c.flush())).status).toBe("offline")
  await context.setOffline(false)
  // Offline detection differs by engine. Any abandoned preparation must outlive
  // its persisted reservation on the injected clock, without inventing a grant.
  await page.evaluate(() => { window.now += 2000 })
  await expect.poll(async () => (await snapshot(request, appId)).posts).toBe(1)
  await expect.poll(async () => (await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
  const received = await snapshot(request, appId)
  expect(received.accepted).toBe(1)
  expect(received.requests[0].events[0].event_id === stored.eventIds[0]).toBe(true)
})

test("simulated pagehide/pageshow and freeze/resume invalidate permission (not a BFCache claim)", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId })) return
  await page.evaluate(() => c.confirmConsent(() => true))
  expect((await page.evaluate(() => c.track({ eventName: "before_suspend" }))).status).toBe("durablyStored")
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })))
  expect((await page.evaluate(() => c.getDiagnostics())).consent).toBe("unknown")
  expect((await page.evaluate(() => c.confirmConsent(() => true))).status).toBe("consentUnknown")
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })))
  expect(await page.evaluate(() => permissionRequired)).toBe(1)
  expect((await page.evaluate(() => c.track({ eventName: "no_restored_grant" }))).status).toBe("consentUnknown")
  expect((await snapshot(request, appId)).posts).toBe(0)
  await page.evaluate(() => c.confirmConsent(() => true))
  await page.evaluate(() => document.dispatchEvent(new Event("freeze")))
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  await page.evaluate(() => document.dispatchEvent(new Event("resume")))
  expect(await page.evaluate(() => permissionRequired)).toBe(2)
  expect((await page.evaluate(() => c.getDiagnostics())).consent).toBe("unknown")
  await page.evaluate(() => c.confirmConsent(() => true))
  await page.evaluate(() => c.flush())
  expect((await snapshot(request, appId)).posts).toBe(1)
})

test("external IndexedDB deletion invalidates the old client and creates a new installation", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId })) return
  await page.evaluate(() => c.confirmConsent(() => true))
  await page.evaluate(() => c.track({ eventName: "old_installation" }))
  await page.evaluate(() => c.flush())
  const before = await snapshot(request, appId)
  await page.evaluate(name => new Promise((resolve, reject) => {
    const deletion = indexedDB.deleteDatabase(name)
    deletion.onsuccess = () => resolve()
    deletion.onerror = () => reject(new Error("testDatabaseDeletionFailed"))
  }), databaseName(appId))
  const invalidated = await page.evaluate(() => c.getDiagnostics())
  expect(invalidated.storageAvailable).toBe(false)
  expect(invalidated.storageCode).toBe("storageVersionChanged")
  expect((await page.evaluate(() => c.track({ eventName: "stale_client" }))).status).toBe("consentUnknown")
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  await page.evaluate(() => c.dispose())
  if (!await installClient(page, testInfo, { appId }, { navigate: false })) return
  expect((await page.evaluate(() => c.getDiagnostics())).consent).toBe("unknown")
  await page.evaluate(() => c.confirmConsent(() => true))
  await page.evaluate(() => c.track({ eventName: "new_installation" }))
  await page.evaluate(() => c.flush())
  const after = await snapshot(request, appId)
  expect(after.posts).toBe(2)
  expect(after.requests[1].events[0].user.user_pseudo_id !== before.requests[0].events[0].user.user_pseudo_id).toBe(true)
})

test("a real IndexedDB versionchange closes active storage without silently upgrading", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId })) return
  await page.evaluate(() => c.confirmConsent(() => true))
  await page.evaluate(name => new Promise((resolve, reject) => {
    const upgrade = indexedDB.open(name, 2)
    upgrade.onsuccess = () => { upgrade.result.close(); resolve() }
    upgrade.onerror = () => reject(new Error("testDatabaseUpgradeFailed"))
  }), databaseName(appId))
  expect((await page.evaluate(() => c.getDiagnostics())).storageAvailable).toBe(false)
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  await page.evaluate(() => c.dispose())
  const code = await page.evaluate(async appId => {
    try { await window.install({ appId }); return "unexpectedSuccess" }
    catch (error) { return error.code }
  }, appId)
  expect(code).toBe("storageUnsupportedVersion")
  expect((await snapshot(request, appId)).posts).toBe(0)
})

test("malformed persisted metadata fails closed without resetting the database", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId })) return
  await page.evaluate(() => c.dispose())
  await page.evaluate(name => new Promise((resolve, reject) => {
    const open = indexedDB.open(name, 1)
    open.onerror = () => reject(new Error("testDatabaseOpenFailed"))
    open.onsuccess = () => {
      const db = open.result
      const tx = db.transaction("meta", "readwrite")
      tx.objectStore("meta").put({ schemaVersion: 999 }, "state")
      tx.oncomplete = () => { db.close(); resolve() }
      tx.onabort = () => { db.close(); reject(new Error("testMetadataWriteFailed")) }
    }
  }), databaseName(appId))
  const result = await page.evaluate(async appId => {
    try { await window.install({ appId }); return "unexpectedSuccess" }
    catch (error) { return error.code }
  }, appId)
  expect(result).toBe("storageUnsupportedSchema")
  const persisted = await page.evaluate(name => new Promise(resolve => {
    const open = indexedDB.open(name)
    open.onsuccess = () => {
      const db = open.result
      const get = db.transaction("meta").objectStore("meta").get("state")
      get.onsuccess = () => { db.close(); resolve(get.result.schemaVersion) }
    }
  }), databaseName(appId))
  expect(persisted).toBe(999)
  expect((await snapshot(request, appId)).posts).toBe(0)
})

test("endpoint migration is explicit, requires permission again, and preserves frozen IDs and identity", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId })) return
  await page.evaluate(() => c.confirmConsent(() => true))
  await page.evaluate(() => c.track({ eventName: "identity_baseline" }))
  await page.evaluate(() => c.flush())
  const baseline = await snapshot(request, appId)
  const stored = await page.evaluate(() => c.track({ eventName: "pending_migration" }))
  expect(stored.status).toBe("durablyStored")
  await page.evaluate(() => c.dispose())
  const missingMigration = await page.evaluate(async config => {
    try { await window.install(config); return "unexpectedSuccess" }
    catch (error) { return error.code }
  }, { appId, collectorUrl: endpoint(appId, "b") })
  expect(missingMigration).toBe("endpointMigrationRequired")
  if (!await installClient(page, testInfo, { appId, collectorUrl: endpoint(appId, "b"), previousCollectorUrl: endpoint(appId) }, { navigate: false })) return
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(1)
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  expect((await snapshot(request, appId)).posts).toBe(1)
  await page.evaluate(() => c.confirmConsent(() => true))
  await page.evaluate(() => c.flush())
  const migrated = await snapshot(request, appId)
  expect(migrated.posts).toBe(2)
  expect(migrated.requests[1].destination).toBe("b")
  expect(migrated.requests[1].events[0].event_id === stored.eventIds[0]).toBe(true)
  expect(migrated.requests[1].events[0].user.user_pseudo_id === baseline.requests[0].events[0].user.user_pseudo_id).toBe(true)
})

test("a failed queue purge is durably blocked across a real page restart", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId, lockTimeoutMs: 150 })) return
  await page.evaluate(() => c.confirmConsent(() => true))
  const old = await page.evaluate(() => c.track({ eventName: "must_not_replay_after_failed_withdrawal" }))
  expect(old.status).toBe("durablyStored")

  const gate = await context.newPage()
  await gate.goto("http://127.0.0.1:4173/")
  await gate.evaluate(appId => {
    window.releaseStateGate = null
    window.stateGateHeld = new Promise(resolve => { window.signalStateGateHeld = resolve })
    window.stateGatePromise = navigator.locks.request(
      `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test:state`,
      async () => { window.signalStateGateHeld(); await new Promise(resolve => { window.releaseStateGate = resolve }) },
    )
  }, appId)
  await gate.evaluate(() => window.stateGateHeld)

  const failed = await page.evaluate(() => c.withdrawConsent())
  expect(failed.globalWithdrawalCommitted).toBe(false)
  expect(failed.localDisabled).toBe(true)
  const pendingBeforeRestart = await page.evaluate(appId =>
    sdk.createLocalStorageRevocationStore({ appId, environment: "browser-test" }).readPending(), appId)
  expect(typeof pendingBeforeRestart).toBe("string")
  await gate.evaluate(() => window.releaseStateGate())
  await gate.evaluate(() => window.stateGatePromise)
  await page.evaluate(() => c.dispose())
  await page.reload()
  if (!await installClient(page, testInfo, { appId, lockTimeoutMs: 150 }, { navigate: false })) return

  await page.evaluate(() => { window.permissionReads = 0 })
  const blocked = await page.evaluate(async () => c.confirmConsent(() => { window.permissionReads++; return true }))
  expect(blocked.status).toBe("consentUnknown")
  expect(blocked.safeDiagnosticCode).toBe("withdrawalIncomplete")
  expect(await page.evaluate(() => window.permissionReads)).toBe(0)
  expect((await page.evaluate(() => c.getDiagnostics())).withdrawalIncomplete).toBe(true)
  expect((await page.evaluate(() => c.track({ eventName: "blocked" }))).status).toBe("consentUnknown")
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  expect((await snapshot(request, appId)).posts).toBe(0)
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(0)

  const repaired = await page.evaluate(() => c.withdrawConsent())
  expect(repaired.globalWithdrawalCommitted).toBe(true)
  expect(repaired.revocationCleared).toBe(true)
  expect(await page.evaluate(appId =>
    sdk.createLocalStorageRevocationStore({ appId, environment: "browser-test" }).readPending(), appId)).toBe(null)
  await page.evaluate(() => c.confirmConsent(() => true))
  const fresh = await page.evaluate(() => c.track({ eventName: "after_durable_revocation_repair" }))
  await page.evaluate(() => c.flush())
  const delivered = await snapshot(request, appId)
  expect(delivered.posts).toBe(1)
  expect(delivered.requests[0].events.map(event => event.event_id)).toEqual(fresh.eventIds)
  expect(delivered.requests.flatMap(record => record.events).some(event => event.event_id === old.eventIds[0])).toBe(false)
})
