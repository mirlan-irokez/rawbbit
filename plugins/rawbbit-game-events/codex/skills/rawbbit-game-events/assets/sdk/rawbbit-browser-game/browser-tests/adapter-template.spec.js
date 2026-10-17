import { randomUUID } from "node:crypto"
import { test, expect } from "@playwright/test"
import { APP_ORIGIN, configure, snapshot } from "./server.mjs"

const appScope = () => `test-${randomUUID()}`

async function installAdapter(page, appId, lockTimeoutMs = 100, { delayHostFetchRegistration = false } = {}) {
  await page.goto(APP_ORIGIN)
  return page.evaluate(async ({ appId, lockTimeoutMs, delayHostFetchRegistration }) => {
    const { createGameAnalyticsAdapter } = await import("/adapter.js")
    const permissionKey = `rawbbit-adapter-permission:${appId}`
    const denialPendingKey = `rawbbit-adapter-denial-pending:${appId}`
    const generationKey = `rawbbit-adapter-permission-generation:${appId}`
    localStorage.setItem(permissionKey, "granted")
    if (localStorage.getItem(generationKey) === null) localStorage.setItem(generationKey, "0")
    const isAllowed = () => localStorage.getItem(permissionKey) === "granted" &&
      localStorage.getItem(denialPendingKey) !== "1"
    let hostRequestCount = 0
    let delayedRequestNumber = null
    let signalDelayedReady = null
    const nativeLocks = navigator.locks
    const locks = delayHostFetchRegistration ? {
      request(name, options, callback) {
        if (name.endsWith(":host-consent")) {
          hostRequestCount += 1
          if (hostRequestCount === delayedRequestNumber) {
            delayedRequestNumber = null
            return new Promise((resolve, reject) => {
              let started = false
              const abort = () => {
                if (started) return
                options.signal?.removeEventListener("abort", abort)
                reject(options.signal?.reason ?? new DOMException("Aborted", "AbortError"))
              }
              window.releaseDelayedHostLock = () => {
                if (started) return
                if (options.signal?.aborted) return abort()
                started = true
                options.signal?.removeEventListener("abort", abort)
                nativeLocks.request(name, options, async lock => {
                  window.delayedHostCallbackEntered = true
                  window.delayedHostSignalAborted = options.signal?.aborted === true
                  try { return await callback(lock) }
                  catch (error) {
                    window.delayedHostCallbackError = String(error?.message ?? error)
                    throw error
                  }
                }).then(resolve, reject)
              }
              options.signal?.addEventListener("abort", abort, { once: true })
              signalDelayedReady?.()
              signalDelayedReady = null
            })
          }
        }
        return nativeLocks.request(name, options, callback)
      },
    } : nativeLocks
    if (delayHostFetchRegistration) {
      window.hostLockTestControl = {
        delayFetchRegistration() {
          delayedRequestNumber = hostRequestCount + 2 // adapter flush check, then actual SDK Fetch check
          return new Promise(resolve => { signalDelayedReady = resolve })
        },
      }
    }

    window.adapter = await createGameAnalyticsAdapter({
      config: {
        collectorUrl: `http://127.0.0.1:4174/${appId}/a`,
        apiKey: "synthetic-browser-test-key",
        appId,
        environment: "browser-test",
        allowInsecureHttpForDevelopment: true,
        autoFlush: false,
        timeoutMs: delayHostFetchRegistration ? 20000 : 1000,
        lockTimeoutMs,
        storageTimeoutMs: 1000,
        flushTimeoutMs: 1500,
        BroadcastChannel: null,
        ...(delayHostFetchRegistration ? { locks } : {}),
      },
      readCurrentAnalyticsPermission: async () => isAllowed(),
      readAnalyticsPermissionGeneration: () => Number(localStorage.getItem(generationKey)),
      isAnalyticsPermissionAllowed: isAllowed,
      persistAnalyticsPermissionDenied: () => {
        localStorage.setItem(denialPendingKey, "1")
        localStorage.setItem(generationKey, String(Number(localStorage.getItem(generationKey)) + 1))
        localStorage.setItem(permissionKey, "denied")
        if (window.deferHostDenial === true) {
          return new Promise(resolve => {
            window.releaseHostDenial = () => {
              localStorage.removeItem(denialPendingKey)
              resolve()
            }
          })
        }
        localStorage.removeItem(denialPendingKey)
      },
    })
    return { supported: typeof navigator.locks?.request === "function" }
  }, { appId, lockTimeoutMs, delayHostFetchRegistration })
}

test("host gate blocks a granted peer after pre-marker SDK lock timeout and repair purges old events", async ({ page, context, request }) => {
  const appId = appScope()
  await configure(request, appId)
  const peer = await context.newPage()
  try {
    expect((await installAdapter(page, appId)).supported).toBe(true)
    expect((await installAdapter(peer, appId)).supported).toBe(true)

    expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.trackCommittedTransition("before_failed_withdrawal", {}))).status)
      .toBe("durablyStored")

    const revocationLock = `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test:revocation`
    await page.evaluate(lockName => {
      let resolveEntered, resolveRelease
      window.revocationLockEntered = new Promise(resolve => { resolveEntered = resolve })
      const release = new Promise(resolve => { resolveRelease = resolve })
      window.releaseRevocationLock = resolveRelease
      window.heldRevocationLock = navigator.locks.request(lockName, async () => {
        resolveEntered()
        await release
      })
    }, revocationLock)
    await page.evaluate(() => revocationLockEntered)

    const failed = await page.evaluate(() => adapter.withdrawConsent())
    expect(failed.globalWithdrawalCommitted).toBe(false)
    expect(failed.revocationCleared).toBe(false)
    expect(await peer.evaluate(() => adapter.trackCommittedTransition("must_not_admit", {})))
      .toMatchObject({ status: "consentWithdrawn" })
    expect(await peer.evaluate(() => adapter.flush())).toMatchObject({ status: "paused" })
    expect((await snapshot(request, appId)).posts).toBe(0)

    await page.evaluate(() => {
      releaseRevocationLock()
      return heldRevocationLock
    })
    const repaired = await page.evaluate(() => adapter.withdrawConsent())
    expect(repaired.globalWithdrawalCommitted).toBe(true)
    expect(repaired.revocationCleared).toBe(true)
    expect(await peer.evaluate(() => adapter.getDiagnostics().then(value => value.pendingCount))).toBe(0)

    const permissionKey = `rawbbit-adapter-permission:${appId}`
    await page.evaluate(key => localStorage.setItem(key, "granted"), permissionKey)
    expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.trackCommittedTransition("after_reconsent", {}))).status)
      .toBe("durablyStored")
    await peer.evaluate(() => adapter.flush())

    const collector = await snapshot(request, appId)
    expect(collector.posts).toBe(1)
    expect(collector.requests[0].events.map(event => event.event_name))
      .toEqual(["session_started", "after_reconsent"])
  } finally {
    await peer.close()
  }
})

async function queueFetchBehindHostLock(page, appId, timeoutMs) {
  const lockName = `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test:host-consent`
  await page.evaluate(async ({ lockName, timeoutMs }) => {
    let releaseFirst, resolveFirstEntered, releaseBarrier, resolveBarrierEntered
    const firstEntered = new Promise(resolve => { resolveFirstEntered = resolve })
    const firstRelease = new Promise(resolve => { releaseFirst = resolve })
    const barrierEntered = new Promise(resolve => { resolveBarrierEntered = resolve })
    const barrierRelease = new Promise(resolve => { releaseBarrier = resolve })
    window.firstHostLock = navigator.locks.request(lockName, async () => {
      resolveFirstEntered()
      await firstRelease
    })
    await firstEntered

    window.barrierFlush = adapter.flush({ timeoutMs })
    window.barrierLock = navigator.locks.request(lockName, async () => {
      resolveBarrierEntered()
      await barrierRelease
    })
    releaseFirst()
    await barrierEntered

    const deadline = Date.now() + 1500
    let fetchWaiterQueued = false
    while (Date.now() < deadline) {
      const state = await navigator.locks.query()
      fetchWaiterQueued = state.pending.some(lock => lock.name === lockName)
      if (fetchWaiterQueued) break
      await new Promise(resolve => setTimeout(resolve, 5))
    }
    if (!fetchWaiterQueued) throw new Error("SDK Fetch did not wait on the host-consent lock")
    window.releaseHostLockBarrier = releaseBarrier
    return true
  }, { lockName, timeoutMs })
}

test("a Fetch queued before withdrawal cannot send its purged batch after immediate re-consent", async ({ page, request }) => {
  const appId = appScope()
  await configure(request, appId)
  await installAdapter(page, appId, 2000)
  expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
  expect((await page.evaluate(() => adapter.trackCommittedTransition("must_be_purged", {}))).status)
    .toBe("durablyStored")

  await queueFetchBehindHostLock(page, appId, 2000)
  const started = await page.evaluate(appId => {
    window.adapterWithdrawal = adapter.withdrawConsent()
    return localStorage.getItem(`rawbbit-adapter-permission:${appId}`)
  }, appId)
  // The callback closes the authoritative permission synchronously before the
  // host lock can drain either the queued Fetch or withdrawal operation.
  expect(started).toBe("denied")
  await page.evaluate(() => releaseHostLockBarrier())
  const [flushResult, withdrawalResult] = await page.evaluate(async () => Promise.all([
    window.barrierFlush,
    window.adapterWithdrawal,
  ]))
  expect(flushResult.status).not.toBe("sent")
  expect(withdrawalResult.globalWithdrawalCommitted).toBe(true)
  expect(withdrawalResult.revocationCleared).toBe(true)
  expect((await snapshot(request, appId)).posts).toBe(0)

  await page.evaluate(key => localStorage.setItem(key, "granted"), `rawbbit-adapter-permission:${appId}`)
  expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
  expect((await page.evaluate(() => adapter.trackCommittedTransition("fresh_after_reconsent", {}))).status)
    .toBe("durablyStored")
  await page.evaluate(() => adapter.flush())
  const collector = await snapshot(request, appId)
  expect(collector.posts).toBe(1)
  expect(collector.requests[0].events.map(event => event.event_name))
    .toEqual(["session_started", "fresh_after_reconsent"])
})

test("calls during a deferred host denial cannot become admissions after re-consent", async ({ page, context, request }) => {
  const appId = appScope()
  await configure(request, appId)
  const peer = await context.newPage()
  try {
    await installAdapter(page, appId)
    await installAdapter(peer, appId)
    expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")

    const denied = await page.evaluate(appId => {
      window.deferHostDenial = true
      window.deferredWithdrawal = adapter.withdrawConsent()
      return {
        permission: localStorage.getItem(`rawbbit-adapter-permission:${appId}`),
        pending: localStorage.getItem(`rawbbit-adapter-denial-pending:${appId}`),
      }
    }, appId)
    // The second adapter observes the synchronous shared denial gate while
    // persistence remains pending, and returns instead of queueing admission.
    expect(denied).toEqual({ permission: "denied", pending: "1" })
    expect(await peer.evaluate(() => adapter.trackCommittedTransition("during_denial", {})))
      .toMatchObject({ status: "consentWithdrawn" })
    await page.evaluate(() => releaseHostDenial())
    const withdrawal = await page.evaluate(() => window.deferredWithdrawal)
    expect(withdrawal.globalWithdrawalCommitted).toBe(true)
    expect(withdrawal.revocationCleared).toBe(true)
    expect(await peer.evaluate(() => adapter.getDiagnostics().then(value => value.pendingCount))).toBe(0)

    await page.evaluate(key => localStorage.setItem(key, "granted"), `rawbbit-adapter-permission:${appId}`)
    expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.trackCommittedTransition("fresh_after_denial", {}))).status)
      .toBe("durablyStored")
    await peer.evaluate(() => adapter.flush())
    const collector = await snapshot(request, appId)
    expect(collector.posts).toBe(1)
    expect(collector.requests[0].events.map(event => event.event_name))
      .toEqual(["session_started", "fresh_after_denial"])
  } finally {
    await peer.close()
  }
})

test("permission generation fences a Fetch lock request registered after withdrawal and re-consent", async ({ page, context, request }) => {
  const appId = appScope()
  await configure(request, appId)
  const peer = await context.newPage()
  try {
    await installAdapter(page, appId)
    await installAdapter(peer, appId, 30000, { delayHostFetchRegistration: true })
    expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.trackCommittedTransition("must_not_escape_purge", {}))).status)
      .toBe("durablyStored")

    await peer.evaluate(async () => {
      window.delayedRegistrationReady = hostLockTestControl.delayFetchRegistration()
      window.delayedOldFlush = adapter.flush({ timeoutMs: 20000 })
      await window.delayedRegistrationReady
    })

    const withdrawal = await page.evaluate(() => adapter.withdrawConsent())
    expect(withdrawal.globalWithdrawalCommitted).toBe(true)
    expect(withdrawal.revocationCleared).toBe(true)
    await page.evaluate(appId => {
      localStorage.setItem(`rawbbit-adapter-permission:${appId}`, "granted")
    }, appId)
    expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")

    await peer.evaluate(() => releaseDelayedHostLock())
    expect((await peer.evaluate(() => window.delayedOldFlush)).status).not.toBe("sent")
    const delayedCallback = await peer.evaluate(() => ({
      entered: window.delayedHostCallbackEntered === true,
      signalAborted: window.delayedHostSignalAborted,
      error: window.delayedHostCallbackError,
    }))
    expect(delayedCallback.entered).toBe(true)
    expect(delayedCallback.signalAborted).toBe(false)
    expect(delayedCallback.error).toContain("Host analytics permission changed before Fetch start")
    expect((await snapshot(request, appId)).posts).toBe(0)
    expect(await peer.evaluate(() => adapter.getDiagnostics().then(value => value.pendingCount))).toBe(0)

    expect((await peer.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
    expect((await peer.evaluate(() => adapter.trackCommittedTransition("fresh_after_generation_fence", {}))).status)
      .toBe("durablyStored")
    await peer.evaluate(() => adapter.flush())
    const collector = await snapshot(request, appId)
    expect(collector.posts).toBe(1)
    expect(collector.requests[0].events.map(event => event.event_name))
      .toEqual(["session_started", "fresh_after_generation_fence"])
  } finally {
    await peer.close()
  }
})

test("adapter snapshots event data synchronously before host-lock waits", async ({ page, request }) => {
  const appId = appScope()
  await configure(request, appId)
  await installAdapter(page, appId)
  expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
  const lockName = `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test:host-consent`
  await page.evaluate(async lockName => {
    let release, resolveEntered
    const entered = new Promise(resolve => { resolveEntered = resolve })
    const blocked = new Promise(resolve => { release = resolve })
    window.snapshotBlocker = navigator.locks.request(lockName, async () => {
      resolveEntered()
      await blocked
    })
    await entered
    const properties = { score: 1 }
    window.snapshotTrack = adapter.trackCommittedTransition("snapshot_at_call", properties)
    properties.score = 99
    const batchProperties = { score: 3 }
    const batch = [{ eventName: "batch_snapshot_at_call", eventProperties: batchProperties }]
    window.snapshotBatch = adapter.trackCommittedTransitions(batch)
    batchProperties.score = 101
    batch[0].eventName = "mutated_batch_name"
    batch.push({ eventName: "late_batch_item", eventProperties: {} })
    release()
    await Promise.all([window.snapshotTrack, window.snapshotBatch])
    await window.snapshotBlocker
  }, lockName)
  await page.evaluate(() => adapter.flush())
  const collector = await snapshot(request, appId)
  expect(collector.posts).toBe(1)
  const event = collector.requests[0].events.find(item => item.event_name === "snapshot_at_call")
  expect(event.event_params.score).toBe(1)
  const batchEvent = collector.requests[0].events.find(item => item.event_name === "batch_snapshot_at_call")
  expect(batchEvent.event_params.score).toBe(3)
  expect(collector.requests[0].events.some(item => item.event_name === "late_batch_item")).toBe(false)
})

test("flush timeout bounds and cancels its initial host-lock wait", async ({ page, request }) => {
  const appId = appScope()
  await configure(request, appId)
  await installAdapter(page, appId, 2000)
  expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
  expect((await page.evaluate(() => adapter.trackCommittedTransition("wait_for_flush", {}))).status)
    .toBe("durablyStored")
  const lockName = `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test:host-consent`
  await page.evaluate(async lockName => {
    let release, resolveEntered
    const entered = new Promise(resolve => { resolveEntered = resolve })
    const held = new Promise(resolve => { release = resolve })
    window.initialFlushBlocker = navigator.locks.request(lockName, async () => {
      resolveEntered()
      await held
    })
    await entered
    window.initialWaitFlush = adapter.flush({ timeoutMs: 80 })

    const deadline = Date.now() + 1000
    let waiterQueued = false
    while (Date.now() < deadline) {
      const state = await navigator.locks.query()
      waiterQueued = state.pending.some(lock => lock.name === lockName)
      if (waiterQueued) break
      await new Promise(resolve => setTimeout(resolve, 5))
    }
    if (!waiterQueued) throw new Error("adapter flush did not queue its initial host-lock check")
    window.releaseInitialFlushBlocker = release
  }, lockName)

  expect((await page.evaluate(() => window.initialWaitFlush)).status).toBe("timeout")
  const pendingCount = await page.evaluate(async lockName => {
    const state = await navigator.locks.query()
    return state.pending.filter(lock => lock.name === lockName).length
  }, lockName)
  expect(pendingCount).toBe(0)
  await page.evaluate(() => releaseInitialFlushBlocker())
  await page.evaluate(() => window.initialFlushBlocker)
  expect((await snapshot(request, appId)).posts).toBe(0)
})

test("an aborted adapter flush removes its queued host-lock Fetch request", async ({ page, request }) => {
  const appId = appScope()
  await configure(request, appId)
  await installAdapter(page, appId, 2000)
  expect((await page.evaluate(() => adapter.confirmCurrentPermission())).status).toBe("confirmed")
  await page.evaluate(() => adapter.trackCommittedTransition("aborted_fetch", {}))
  await queueFetchBehindHostLock(page, appId, 500)
  const flushResult = await page.evaluate(() => window.barrierFlush)
  expect(flushResult.status).toBe("timeout")
  const pendingCount = await page.evaluate(async appId => {
    const lockName = `rawbbit-browser-game:${encodeURIComponent(appId)}:browser-test:host-consent`
    const state = await navigator.locks.query()
    return state.pending.filter(lock => lock.name === lockName).length
  }, appId)
  expect(pendingCount).toBe(0)
  await page.evaluate(() => releaseHostLockBarrier())
  await page.evaluate(() => window.barrierLock)
  expect((await snapshot(request, appId)).posts).toBe(0)
})
