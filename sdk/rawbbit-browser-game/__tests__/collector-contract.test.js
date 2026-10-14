import test from "node:test"
import assert from "node:assert/strict"

import { buildCollectorEvent } from "../vendor/rawbbit-client/normalize.js"
import { performCollectorRequest, resolveCollectorEndpoint } from "../vendor/rawbbit-client/transport.js"

test("portable Collector envelope fixture preserves the reviewed wire shape", async () => {
  const event = await buildCollectorEvent({
    eventInput: {
      event_name: "round_completed",
      event_id: "event-fixed-1",
      event_timestamp: "2026-10-13T12:34:56.000Z",
      user: { user_id: "account-7", user_pseudo_id: "approved-player" },
      device: { appVersion: "1.2.0" },
      event_params: { score: 4200, product: "event-override" },
      user_properties: { cohort: "early" },
      traffic_source: { source: "game" },
      geo: { country_code: "US" },
      consent: { analytics_storage: true },
    },
    config: {
      appId: "com.example.game",
      environment: "prod",
      product: "example-game",
      surface: "browser_game",
      sourceType: "game_client",
      schemaVersion: 1,
      defaultDevice: { platform: "web", locale: "en-US" },
    },
    identityAdapter: {
      getUserPseudoId: async () => "fallback-player",
      getSessionId: async () => "session-fixed-1",
    },
  })

  assert.deepEqual(event, {
    event_id: "event-fixed-1",
    app_id: "com.example.game",
    environment: "prod",
    event_name: "round_completed",
    event_timestamp: "2026-10-13T12:34:56.000Z",
    user: { user_id: "account-7", user_pseudo_id: "approved-player", session_id: "session-fixed-1" },
    device: {
      platform: "web",
      app_version: "1.2.0",
      os_version: null,
      device_model: null,
      locale: "en-US",
      timezone: null,
    },
    event_params: {
      product: "event-override",
      surface: "browser_game",
      source_type: "game_client",
      schema_version: 1,
      score: 4200,
    },
    user_properties: { cohort: "early" },
    traffic_source: { source: "game" },
    geo: { country_code: "US" },
    consent: { analytics_storage: true },
  })
})

test("portable Collector HTTP fixture starts one bounded POST with the exact request contract", async () => {
  const payload = '{"events":[{"event_id":"event-fixed-1","app_id":"com.example.game","event_name":"round_completed"}]}'
  let captured
  const pending = performCollectorRequest({
    collectorUrl: " https://collector.example/ ",
    apiKey: "test-app-ingest-key",
    payload,
    timeoutMs: 1000,
    maxResponseBytes: 128,
    fetchImpl(url, init) {
      captured = { url, init }
      return Promise.resolve(new Response('{"accepted_events":1}', {
        status: 200,
        headers: { "Retry-After": "2" },
      }))
    },
  })

  assert.ok(captured, "fetch is invoked synchronously before the request promise suspends")
  assert.equal(captured.url, "https://collector.example/v1/events:batch")
  assert.equal(captured.init.method, "POST")
  assert.deepEqual(captured.init.headers, {
    "Content-Type": "application/json",
    "X-API-Key": "test-app-ingest-key",
  })
  assert.equal(captured.init.body, payload)
  assert.equal(captured.init.keepalive, false)
  assert.equal(captured.init.redirect, "error")
  assert.equal(captured.init.credentials, "omit")
  assert.equal(captured.init.referrerPolicy, "no-referrer")
  assert.deepEqual(await pending, {
    ok: true,
    status: 200,
    body: { accepted_events: 1 },
    retryAfter: "2",
    networkError: false,
    cancelled: false,
    bodyError: false,
  })
  assert.equal(resolveCollectorEndpoint("https://collector.example/V1/EVENTS:BATCH/"),
    "https://collector.example/V1/EVENTS:BATCH")
})

test("the modern Collector primitive leaves retry decisions to its caller", async () => {
  let calls = 0
  await assert.rejects(performCollectorRequest({
    collectorUrl: "https://collector.example",
    apiKey: "test-app-ingest-key",
    payload: '{"events":[]}',
    fetchImpl() {
      calls += 1
      return Promise.reject(new TypeError("synthetic network failure"))
    },
  }), /synthetic network failure/)
  assert.equal(calls, 1)
})
