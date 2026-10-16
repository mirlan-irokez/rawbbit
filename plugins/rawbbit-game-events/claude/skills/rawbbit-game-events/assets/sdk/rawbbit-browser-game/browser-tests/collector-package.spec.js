import { randomUUID } from "node:crypto"
import { test, expect } from "@playwright/test"
import { APP_ORIGIN, configure, installClient, snapshot } from "./server.mjs"

const scope = () => `test-${randomUUID()}`
const grantAndTrack = async page => {
  expect((await page.evaluate(() => c.confirmConsent(() => true))).status).toBe("confirmed")
  const stored = await page.evaluate(() => c.track({ eventName: "synthetic_action", eventParams: { checkpoint: 4 } }))
  expect(stored.status).toBe("durablyStored")
  return stored
}

test("the browser imports only the built public ESM entry, with no sibling source requests", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  const scripts = []
  page.on("request", req => { if (req.resourceType() === "script") scripts.push(new URL(req.url()).pathname) })
  const response = await request.get(`${APP_ORIGIN}/sdk.js`)
  expect(response.ok()).toBe(true)
  const source = await response.text()
  expect(/(?:from\s*|import\s*\()\s*["']\.\.?\//.test(source)).toBe(false)
  const supported = await installClient(page, testInfo, { appId })
  expect(scripts).toEqual(["/sdk.js"])
  expect(await page.evaluate(() => typeof sdk.createBrowserGameClient)).toBe("function")
  if (!supported) return
  expect((await page.evaluate(() => c.getDiagnostics())).consent).toBe("unknown")
  expect((await page.evaluate(() => c.track({ eventName: "unknown_permission" }))).status).toBe("consentUnknown")
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  expect((await snapshot(request, appId)).posts).toBe(0)
})

test("native capability gating and missing Web Locks explicitly report unsupportedBrowser without fallback", async ({ page, request }) => {
  const appId = scope()
  await configure(request, appId)
  await page.goto(APP_ORIGIN)
  const native = await page.evaluate(async appId => {
    const hasLocks = typeof navigator.locks?.request === "function"
    try { await window.install({ appId }); c.dispose(); return { hasLocks, supported: true } }
    catch (error) { return { hasLocks, supported: false, code: error.code } }
  }, appId)
  expect(native.supported).toBe(native.hasLocks)
  if (!native.hasLocks) expect(native.code).toBe("unsupportedBrowser")
  const code = await page.evaluate(async appId => {
    try { await window.install({ appId, runtime: { locks: {} } }); return "unexpectedSuccess" }
    catch (error) { return error.code }
  }, appId)
  expect(code).toBe("unsupportedBrowser")
  expect((await snapshot(request, appId)).posts).toBe(0)
})

test("real cross-origin OPTIONS permits Content-Type and X-API-Key; fetch omits cookies and referrer", async ({ page, context, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  await context.addCookies([{ name: "synthetic_cookie", value: "not-for-collector", url: APP_ORIGIN }])
  if (!await installClient(page, testInfo, { appId })) return
  await grantAndTrack(page)
  await page.evaluate(() => c.flush())
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(1)
  expect(received.options).toBeGreaterThanOrEqual(1)
  expect(received.preflights[0].originAllowed).toBe(true)
  expect(received.preflights[0].method).toBe("POST")
  expect(received.preflights[0].headers).toEqual(["content-type", "x-api-key"])
  expect(received.requests[0].keyValid).toBe(true)
  expect(received.requests[0].cookiePresent).toBe(false)
  expect(received.requests[0].referrerPresent).toBe(false)
  expect(await page.evaluate(() => fetchPolicies)).toEqual([{
    method: "POST", keepalive: false, redirect: "error", credentials: "omit", referrerPolicy: "no-referrer",
  }])
})

test("disallowed CORS origin fails preflight and preserves the durable queue", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { allowOrigin: false })
  if (!await installClient(page, testInfo, { appId })) return
  await grantAndTrack(page)
  expect((await page.evaluate(() => c.flush())).status).toBe("pending")
  const received = await snapshot(request, appId)
  expect(received.options).toBeGreaterThanOrEqual(1)
  expect(received.preflights[0].originAllowed).toBe(false)
  expect(received.posts).toBe(0)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(1)
})

test("host CSP connect-src blocks Collector dispatch without losing stored data", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId }, { blocked: true })) return
  await grantAndTrack(page)
  expect((await page.evaluate(() => c.flush())).status).toBe("pending")
  expect((await snapshot(request, appId)).options).toBe(0)
  expect((await snapshot(request, appId)).posts).toBe(0)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(1)
})

for (const exposeRetryAfter of [true, false]) {
  test(`Retry-After is ${exposeRetryAfter ? "readable when exposed" : "invisible without exposure, using bounded fallback"}`, async ({ page, request }, testInfo) => {
    const appId = scope()
    await configure(request, appId, { status: 429, retryAfter: "120", exposeRetryAfter })
    if (!await installClient(page, testInfo, { appId })) return
    await grantAndTrack(page)
    expect((await page.evaluate(() => c.flush())).status).toBe("pending")
    const delay = await page.evaluate(async () => (await c.getDiagnostics()).retryNotBefore - window.now)
    expect(delay).toBe(exposeRetryAfter ? 120000 : 200)
    expect((await page.evaluate(() => c.flush())).safeDiagnosticCode).toBe("retryCooldown")
    expect((await snapshot(request, appId)).posts).toBe(1)
    await configure(request, appId, { status: 200, retryAfter: null })
    await page.evaluate(delay => { window.now += delay + 1 }, delay)
    await page.evaluate(() => c.flush())
    expect((await snapshot(request, appId)).posts).toBe(2)
    expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
  })
}

test("a near-1 MiB request is sent as ordinary fetch, not keepalive/beacon", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId)
  if (!await installClient(page, testInfo, { appId })) return
  await page.evaluate(() => c.confirmConsent(() => true))
  const stored = await page.evaluate(() => c.track({ eventName: "large_snapshot", eventParams: { padding: "x".repeat(1045000) } }))
  expect(stored.status).toBe("durablyStored")
  await page.evaluate(() => c.flush())
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(1)
  expect(received.requests[0].bytes).toBeGreaterThan(1040000)
  expect(received.requests[0].bytes).toBeLessThanOrEqual(1048576)
  expect(await page.evaluate(() => fetchPolicies.every(policy => policy.keepalive === false))).toBe(true)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
})

test("redirects are not followed to another destination and the original event remains queued", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { mode: "redirect" })
  if (!await installClient(page, testInfo, { appId })) return
  await grantAndTrack(page)
  expect((await page.evaluate(() => c.flush())).status).toBe("pending")
  const received = await snapshot(request, appId)
  expect(received.posts).toBe(1)
  expect(received.redirectedPosts).toBe(0)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(1)
})

test("response-body deadline preserves data and schedules retry even after successful headers", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { mode: "body-timeout" })
  if (!await installClient(page, testInfo, { appId, timeoutMs: 250 })) return
  await grantAndTrack(page)
  const started = Date.now()
  const result = await page.evaluate(() => c.flush())
  expect(Date.now() - started).toBeLessThan(4000)
  expect(result.status).toBe("pending")
  const diagnostics = await page.evaluate(() => c.getDiagnostics())
  expect(diagnostics.pendingCount).toBe(1)
  expect(diagnostics.faultCode).toBe(null)
  expect((await snapshot(request, appId)).posts).toBe(1)
})

test("server acceptance with no readable response replays the original frozen event ID", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { mode: "drop" })
  if (!await installClient(page, testInfo, { appId })) return
  const stored = await grantAndTrack(page)
  expect((await page.evaluate(() => c.flush())).status).toBe("pending")
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(1)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(1)
  await configure(request, appId, { mode: "normal" })
  await page.evaluate(() => { window.now += 1000 })
  await page.evaluate(() => c.flush())
  const received = await snapshot(request, appId)
  // Native engines may retransmit a failed HTTP request underneath one fetch call.
  expect(await page.evaluate(() => sdkFetchInvocations)).toBe(2)
  expect(received.posts).toBeGreaterThanOrEqual(2)
  expect(received.accepted).toBe(received.posts)
  expect(received.requests.every(entry => entry.events.length === 1 && entry.events[0].event_id === stored.eventIds[0])).toBe(true)
  expect((await page.evaluate(() => c.getDiagnostics())).pendingCount).toBe(0)
})

test("accepted-count mismatch latches a shared fault without deleting the event", async ({ page, request }, testInfo) => {
  const appId = scope()
  await configure(request, appId, { acceptedCount: 0 })
  if (!await installClient(page, testInfo, { appId })) return
  await grantAndTrack(page)
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  const diagnostics = await page.evaluate(() => c.getDiagnostics())
  expect(diagnostics.faultCode).toBe("acceptedCountMismatch")
  expect(diagnostics.pendingCount).toBe(1)
  expect((await page.evaluate(() => c.flush())).status).toBe("paused")
  expect((await snapshot(request, appId)).posts).toBe(1)
})

for (const status of [400, 401, 403, 404, 408, 413, 422, 429, 500]) {
  test(`Collector HTTP ${status} keeps rejected/unacknowledged data and applies the correct fault policy`, async ({ page, request }, testInfo) => {
    const appId = scope()
    await configure(request, appId, { status })
    if (!await installClient(page, testInfo, { appId })) return
    const stored = await grantAndTrack(page)
    const transient = [408, 429, 500].includes(status)
    expect((await page.evaluate(() => c.flush())).status).toBe(transient ? "pending" : "paused")
    const diagnostics = await page.evaluate(() => c.getDiagnostics())
    expect(diagnostics.pendingCount + diagnostics.quarantinedCount).toBe(1)
    expect(diagnostics.lastHttpStatus).toBe(status)
    expect(diagnostics.quarantinedCount).toBe([400, 413, 422].includes(status) ? 1 : 0)
    expect(Boolean(diagnostics.faultCode)).toBe(!transient)
    expect(await page.evaluate(() => sdkFetchInvocations)).toBe(1)
    const received = await snapshot(request, appId)
    if (status === 408) expect(received.posts).toBeGreaterThanOrEqual(1)
    else expect(received.posts).toBe(1)
    expect(received.requests.every(entry => entry.events.length === 1 && entry.events[0].event_id === stored.eventIds[0])).toBe(true)
  })
}
