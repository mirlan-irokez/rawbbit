import http from "node:http"
import { readFile } from "node:fs/promises"
import { fileURLToPath } from "node:url"
import { test } from "@playwright/test"

export const APP_ORIGIN = "http://127.0.0.1:4173"
export const COLLECTOR_ORIGIN = "http://127.0.0.1:4174"
export const TEST_KEY = "synthetic-browser-test-key"
const bundle = new URL("../dist/index.js", import.meta.url)
const scopePattern = /^test-[a-z0-9-]{1,100}$/
const maxBody = 1048576 + 4096
const defaults = { status: 200, allowOrigin: true, exposeRetryAfter: false, retryAfter: null, mode: "normal", acceptedCount: null }

// The helpers and this HTML are test-owned; no instrumentation is shipped in SDK code.
const html = blocked => `<!doctype html><meta charset="utf-8"><title>Synthetic SDK fixture</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline'; connect-src ${blocked ? "'self'" : `${APP_ORIGIN} ${COLLECTOR_ORIGIN}`}">
<script>
window.install = async function(options) {
  const sdk = await import('/sdk.js');
  window.sdk = sdk;
  window.now = options.now ?? 1800000000000;
  window.permissionRequired = 0;
  window.fetchPolicies = [];
  window.sdkFetchInvocations = 0;
  const { now, prefix, ...config } = options;
  const nativeFetch = window.fetch.bind(window);
  const runtime = {
    ...(config.runtime || {}),
    fetch(url, init) {
      window.sdkFetchInvocations++;
      if (window.fetchPolicies.length < 32) window.fetchPolicies.push({
        method: init.method, keepalive: init.keepalive, redirect: init.redirect,
        credentials: init.credentials, referrerPolicy: init.referrerPolicy
      });
      return nativeFetch(url, init);
    }
  };
  window.c = await sdk.createBrowserGameClient({
    collectorUrl: '${COLLECTOR_ORIGIN}/' + options.appId + '/a',
    apiKey: '${TEST_KEY}', environment: 'browser-test',
    revocationStore: sdk.createLocalStorageRevocationStore({ appId: options.appId, environment: 'browser-test' }),
    allowInsecureHttpForDevelopment: true, autoFlush: false,
    timeoutMs: 1000, backoffMs: 200, random: () => 0.5,
    ...config, runtime, clock: () => window.now,
    onPermissionRequired: () => { window.permissionRequired++; },
    sessionStartFactory: prefix ? () => ({ eventName: 'session_start' }) : undefined
  });
  return { supported: true };
};
</script><p>Local synthetic fixture only.</p>`
const adapterTemplate = new URL("../examples/GameAnalyticsAdapter.js.template", import.meta.url)

export const endpoint = (scope, destination = "a") => `${COLLECTOR_ORIGIN}/${scope}/${destination}`

export async function configure(request, scope, config = {}) {
  const response = await request.put(`${COLLECTOR_ORIGIN}/__test/scopes/${scope}`, { data: config })
  if (!response.ok()) throw new Error("fixtureConfigurationFailed")
}

export async function snapshot(request, scope) {
  const response = await request.get(`${COLLECTOR_ORIGIN}/__test/scopes/${scope}`)
  if (!response.ok()) throw new Error("fixtureSnapshotFailed")
  return response.json()
}

export async function release(request, scope) {
  const response = await request.post(`${COLLECTOR_ORIGIN}/__test/scopes/${scope}/release`)
  if (!response.ok()) throw new Error("fixtureReleaseFailed")
}

export async function installClient(page, testInfo, options, { navigate = true, blocked = false } = {}) {
  if (navigate) await page.goto(`${APP_ORIGIN}/${blocked ? "blocked" : ""}`)
  const capability = await page.evaluate(() => typeof navigator.locks?.request === "function")
  const result = await page.evaluate(async config => {
    try { return await window.install(config) }
    catch (error) { return { supported: false, code: error.code } }
  }, options)
  if (!capability) {
    if (result.supported || result.code !== "unsupportedBrowser") throw new Error("unsupportedBrowserNotReported")
    testInfo.annotations.push({ type: "unsupportedBrowser", description: "Native Web Locks unavailable; actual SDK initialization rejected with unsupportedBrowser. Functional scenario not claimed." })
    test.skip(true, "SDK correctly rejects this browser without native Web Locks; functional scenario not executed")
    return false
  }
  if (!result.supported) throw new Error(`fixtureInitializationFailed:${result.code || "unknown"}`)
  return true
}

async function body(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > maxBody) throw new Error("fixtureBodyLimit")
    chunks.push(chunk)
  }
  return { value: JSON.parse(Buffer.concat(chunks).toString("utf8")), bytes: size }
}

const json = (res, status, value) => {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" })
  res.end(JSON.stringify(value))
}

export async function startServers() {
  const scopes = new Map()
  const pending = new Set()
  let retainedBytes = 0
  const getScope = id => {
    if (!scopes.has(id)) {
      if (scopes.size >= 256) throw new Error("fixtureScopeLimit")
      scopes.set(id, { config: { ...defaults }, options: 0, posts: 0, accepted: 0, redirectedPosts: 0, preflights: [], requests: [], held: new Set() })
    }
    return scopes.get(id)
  }
  const app = http.createServer(async (req, res) => {
    try {
      if (req.headers.host !== "127.0.0.1:4173") return json(res, 400, { code: "localHostRequired" })
      const path = new URL(req.url, APP_ORIGIN).pathname
      if (path === "/__test/health") return json(res, 200, { ready: true })
      if (path === "/sdk.js") {
        res.writeHead(200, { "Content-Type": "text/javascript", "Cache-Control": "no-store" })
        return res.end(await readFile(bundle))
      }
      if (path === "/adapter.js") {
        const source = (await readFile(adapterTemplate, "utf8"))
          .replace('from "@rawbbit/browser-game"', 'from "/sdk.js"')
        res.writeHead(200, { "Content-Type": "text/javascript", "Cache-Control": "no-store" })
        return res.end(source)
      }
      if (path === "/" || path === "/blocked") {
        res.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" })
        return res.end(html(path === "/blocked"))
      }
      json(res, 404, { code: "notFound" })
    } catch { if (!res.headersSent) json(res, 500, { code: "fixtureFailure" }); else res.destroy() }
  })
  const collector = http.createServer(async (req, res) => {
    try {
      // Fixed loopback binding and Host validation: no external Collector or target URLs.
      if (req.headers.host !== "127.0.0.1:4174") return json(res, 400, { code: "localHostRequired" })
      const path = new URL(req.url, COLLECTOR_ORIGIN).pathname
      const management = /^\/__test\/scopes\/(test-[a-z0-9-]+)(\/release)?$/.exec(path)
      if (management && scopePattern.test(management[1])) {
        // Management is unauthenticated and has no CORS allowance for web pages.
        const state = getScope(management[1])
        if (management[2] && req.method === "POST") {
          for (const finish of [...state.held]) finish()
          return json(res, 200, { released: true })
        }
        if (req.method === "PUT" && !management[2]) {
          const { value } = await body(req)
          const config = { ...state.config, ...value }
          if (Object.keys(value).some(key => !Object.hasOwn(defaults, key)) ||
              !Number.isInteger(config.status) || config.status < 200 || config.status > 599 ||
              typeof config.allowOrigin !== "boolean" || typeof config.exposeRetryAfter !== "boolean" ||
              !(config.retryAfter === null || typeof config.retryAfter === "string" && /^[\x20-\x7e]{1,128}$/.test(config.retryAfter)) ||
              !["normal", "hold", "drop", "body-timeout", "redirect"].includes(config.mode) ||
              !(config.acceptedCount === null || Number.isSafeInteger(config.acceptedCount) && config.acceptedCount >= 0)) {
            return json(res, 400, { code: "invalidFixtureConfiguration" })
          }
          state.config = config
          return json(res, 200, { configured: true })
        }
        if (req.method === "GET" && !management[2]) {
          const { options, posts, accepted, redirectedPosts, preflights, requests } = state
          return json(res, 200, { options, posts, accepted, redirectedPosts, preflights, requests })
        }
        return json(res, 405, { code: "methodNotAllowed" })
      }
      const match = /^\/(test-[a-z0-9-]+)\/(a|b|sink)\/v1\/events:batch$/.exec(path)
      if (!match || !scopePattern.test(match[1])) return json(res, 404, { code: "notFound" })
      const state = getScope(match[1])
      const config = { ...state.config }
      const originAllowed = config.allowOrigin && req.headers.origin === APP_ORIGIN
      res.setHeader("Vary", "Origin")
      if (originAllowed) {
        res.setHeader("Access-Control-Allow-Origin", APP_ORIGIN)
        if (config.exposeRetryAfter) res.setHeader("Access-Control-Expose-Headers", "Retry-After")
      }
      if (req.method === "OPTIONS") {
        state.options++
        if (state.preflights.length < 32) state.preflights.push({
          originAllowed, method: req.headers["access-control-request-method"],
          headers: String(req.headers["access-control-request-headers"] || "").toLowerCase().split(",").map(value => value.trim()).sort(),
        })
        res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS")
        res.setHeader("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        res.setHeader("Access-Control-Max-Age", "0")
        res.writeHead(204)
        return res.end()
      }
      if (req.method !== "POST") return json(res, 405, { code: "methodNotAllowed" })
      const { value, bytes } = await body(req)
      // Retain only bounded, deterministic synthetic test payloads, never auth headers.
      if (!Array.isArray(value.events) || value.events.length > 500 ||
          value.events.some(event => event.app_id !== match[1] || event.environment !== "browser-test")) {
        return json(res, 400, { code: "syntheticEventsRequired" })
      }
      state.posts++
      if (match[2] === "sink") state.redirectedPosts++
      if (state.requests.length >= 32 || retainedBytes + bytes > 8 * 1048576) return json(res, 429, { code: "fixtureRequestLimit" })
      retainedBytes += bytes
      state.requests.push({ destination: match[2], bytes, keyValid: req.headers["x-api-key"] === TEST_KEY,
        cookiePresent: Boolean(req.headers.cookie), referrerPresent: Boolean(req.headers.referer), events: value.events })
      if (config.status >= 200 && config.status < 300) state.accepted += value.events.length
      if (config.retryAfter !== null) res.setHeader("Retry-After", config.retryAfter)
      if (config.mode === "drop") return res.destroy()
      if (config.mode === "redirect") {
        res.writeHead(307, { Location: `${COLLECTOR_ORIGIN}/${match[1]}/sink/v1/events:batch` })
        return res.end()
      }
      const reply = () => json(res, config.status, { accepted_events: config.acceptedCount ?? value.events.length })
      if (config.mode === "hold" || config.mode === "body-timeout") {
        let timer
        const finish = () => {
          clearTimeout(timer); state.held.delete(finish); pending.delete(finish)
          if (!res.destroyed) { if (config.mode === "hold") reply(); else res.end(`${config.acceptedCount ?? value.events.length}}`) }
        }
        if (config.mode === "body-timeout") {
          res.writeHead(config.status, { "Content-Type": "application/json" })
          res.write('{"accepted_events":')
        }
        state.held.add(finish); pending.add(finish)
        timer = setTimeout(finish, 5000)
        res.on("close", () => { clearTimeout(timer); state.held.delete(finish); pending.delete(finish) })
        return
      }
      reply()
    } catch { if (!res.headersSent) json(res, 400, { code: "fixtureRequestRejected" }); else res.destroy() }
  })
  const listen = (server, port) => new Promise((resolve, reject) => {
    server.once("error", reject)
    server.listen(port, "127.0.0.1", resolve)
  })
  await Promise.all([listen(app, 4173), listen(collector, 4174)])
  return async () => {
    for (const finish of [...pending]) finish()
    await Promise.all([app, collector].map(server => new Promise(resolve => {
      server.close(resolve); server.closeAllConnections()
    })))
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const stop = await startServers()
  for (const signal of ["SIGINT", "SIGTERM"]) process.once(signal, async () => { await stop(); process.exit(0) })
}
