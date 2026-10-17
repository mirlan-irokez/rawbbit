import assert from "node:assert/strict"
import { IDBFactory } from "fake-indexeddb"
import { createBrowserGameClient } from "../index.js"

export function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

export async function bounded(promise, ms = 1000) {
  let timer
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`Operation exceeded ${ms}ms`)), ms)
    })])
  } finally { clearTimeout(timer) }
}

// Grant callbacks asynchronously, retain exclusive ownership until settlement,
// and cancel only queued requests (never an already granted lock).
export class FakeWebLocks {
  constructor() { this.entries = new Map() }
  held(name) { return Boolean(this.entries.get(name)?.active) }
  request(name, options, callback) {
    if (typeof options === "function") { callback = options; options = {} }
    assert.equal(options.mode ?? "exclusive", "exclusive")
    const entry = this.entries.get(name) ?? { active: false, queue: [] }
    this.entries.set(name, entry)
    if (options.signal?.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"))
    if (options.ifAvailable && (entry.active || entry.queue.length)) {
      return Promise.resolve().then(() => callback(null))
    }
    return new Promise((resolve, reject) => {
      const waiter = { callback, resolve, reject, signal: options.signal }
      waiter.abort = () => {
        const index = entry.queue.indexOf(waiter)
        if (index === -1) return
        entry.queue.splice(index, 1)
        waiter.signal.removeEventListener("abort", waiter.abort)
        reject(new DOMException("Aborted", "AbortError"))
        this.drain(name, entry)
      }
      options.signal?.addEventListener("abort", waiter.abort, { once: true })
      entry.queue.push(waiter)
      this.drain(name, entry)
    })
  }
  drain(name, entry) {
    if (entry.active || !entry.queue.length) return
    const waiter = entry.queue.shift()
    entry.active = true
    waiter.signal?.removeEventListener("abort", waiter.abort)
    Promise.resolve().then(() => waiter.callback({ name, mode: "exclusive" })).then(
      value => { this.release(name, entry); waiter.resolve(value) },
      error => { this.release(name, entry); waiter.reject(error) },
    )
  }
  release(name, entry) { entry.active = false; this.drain(name, entry) }
}

export function collectorResponse(status = 200, body = { accepted_events: 1 }, headers = {}) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status, headers })
}

export function makeHarness(t) {
  const indexedDB = new IDBFactory()
  const locks = new FakeWebLocks()
  let pendingRevocation = null
  let nextRevocation = 0
  const revocationStore = {
    async readPending() { return pendingRevocation },
    async markPending() { pendingRevocation = `host-revocation-${++nextRevocation}`; return pendingRevocation },
    async clearPending(token) {
      if (pendingRevocation !== token) return false
      pendingRevocation = null
      return true
    },
  }
  const clients = new Set()
  const requests = []
  let now = Date.parse("2026-10-13T12:00:00.000Z")
  let fetcher = (_url, init) => collectorResponse(200, { accepted_events: JSON.parse(init.body).events.length })
  const fetch = (url, init) => {
    requests.push({ url, init, events: JSON.parse(init.body).events })
    return fetcher(url, init)
  }
  const tab = () => {
    const window = new EventTarget()
    const document = new EventTarget()
    document.visibilityState = "visible"
    window.self = window; window.top = window
    window.navigator = { onLine: true }; window.document = document
    Object.defineProperty(window, "localStorage", { get() { throw new Error("No localStorage access") } })
    return { window, document, runtime: { window, document, indexedDB, locks,
      crypto: globalThis.crypto, fetch, isSecureContext: true, BroadcastChannel: null } }
  }
  const view = tab()
  const cfg = { collectorUrl: "https://collector.example", appId: "com.example.browser-game",
    environment: "test", apiKey: "private-test-ingest-key", autoFlush: false,
    revocationStore,
    clock: () => now, random: () => 0.5, backoffMs: 1, retryCooldownMs: 10,
    timeoutMs: 100, lockTimeoutMs: 200, storageTimeoutMs: 500, flushTimeoutMs: 200,
    runtime: view.runtime }
  const harness = { cfg, ...view, indexedDB, locks, requests, tab, revocationStore,
    get pendingRevocation() { return pendingRevocation },
    get now() { return now }, set now(value) { now = value },
    advance(ms) { now += ms },
    setFetch(value) { fetcher = value },
    async create(overrides = {}, currentView = view) {
      const client = await bounded(createBrowserGameClient({ ...cfg, runtime: currentView.runtime, ...overrides }))
      clients.add(client)
      return client
    },
    dispose() { for (const client of clients) client.dispose(); clients.clear() },
  }
  t?.after(() => harness.dispose())
  return harness
}

export async function grant(client) {
  assert.equal((await bounded(client.confirmConsent(() => true))).status, "confirmed")
}

export async function stored(client, input = { eventName: "action" }) {
  const value = await bounded(Array.isArray(input) ? client.trackBatch(input) : client.track(input))
  assert.equal(value.status, "durablyStored")
  return value
}

export function readState(client) { return bounded(client.coord.state(() => client.store.read())) }
export async function recoveryOptions(client, overrides = {}) {
  const diagnostics = await bounded(client.getDiagnostics())
  return { expectedFaultRevision: diagnostics.faultRevision, expectedQueueRevision: diagnostics.queueRevision,
    reasonCode: "configurationCorrected", policyVersion: "test-policy-v1", ...overrides }
}
