import { performCollectorRequest } from "./vendor/rawbbit-client/transport.js"
import { uuid, byteLength, serializeBatch, validateFrozen, snapshotRecoveryOptions } from "./payload.js"
import { BrowserGameError } from "./coordinator.js"

const outcome = (status, safeDiagnosticCode = status) => ({ status, safeDiagnosticCode })
const retryAfterMs = (raw, now) => {
  if (typeof raw !== "string" || raw.length > 128) return 0
  let delay = /^\d+$/.test(raw.trim()) ? Number(raw.trim()) * 1000 : Date.parse(raw) - now
  if (!Number.isFinite(delay) || delay < 0) return 0
  return Math.min(delay, 86400000)
}

export class Sender {
  constructor(client) { this.client = client }
  _delay(attempt) {
    const c = this.client
    if (attempt > c.cfg.maxRetries) return c.cfg.retryCooldownMs
    const random = Math.max(0, Math.min(1, c.cfg.random()))
    return Math.min(c.cfg.retryCooldownMs, c.cfg.backoffMs * 2 ** Math.max(0, attempt - 1) * (0.5 + random))
  }

  async attempt(signal) {
    const c = this.client
    if (!c.hasCurrentConsent || c._suspended || c._hidden) return outcome("paused", "consentOrLifecyclePaused")
    if (c.runtime.window.navigator?.onLine === false) return outcome("offline")
    const capability = c._capability
    if (signal?.aborted) return outcome("timeout", "flushDeadline")
    try {
      return await c.coord.sender(async () => {
        c._senderActive = true; c._lastBusy = false
        try {
          const started = await this._start(capability, undefined, signal)
          if (!started.request) return started
          return await this._settle(started)
        } finally { c._senderActive = false }
      })
    } catch (error) { return c._outcome(error) }
  }

  async _start(capability, recovery, signal) {
    const c = this.client
    return c.coord.revocation(async () => {
      let pendingRevocation
      try { pendingRevocation = await c._readPendingRevocation() }
      catch (error) { c._invalidate("consentUnknown"); throw error }
      if (c._withdrawIncomplete || pendingRevocation !== null) {
        c._invalidate("consentUnknown")
        return outcome("paused", "withdrawalIncomplete")
      }
      return c.coord.state(async () => {
      if (signal?.aborted) return outcome("timeout", "flushDeadline")
      let batch
      const early = await c.store.update(state => {
        c._require(state, capability)
        c._prune(state)
        // Sender ownership proves a prior claim has no cooperating live owner.
        // Preserve its reserved budget and deadline across crashes/takeovers.
        state.meta.claim = null
        if (recovery) {
          if (!state.meta.fault || state.meta.fault.revision !== recovery.expectedFaultRevision || state.meta.queueRevision !== recovery.expectedQueueRevision) return outcome("recoveryPending", "staleRecovery")
          if (state.records.some(record => record.quarantined)) return outcome("recoveryPending", "unresolvedQuarantine")
          const invalid = state.records.find(record => !validateFrozen(record.payloadJson, c.cfg))
          if (invalid) { c._latch(state, "invalidFrozenEvent", 0, [invalid.handle]); return outcome("recoveryPending", "invalidFrozenEvent") }
          if (!state.meta.fault.requiresProbe) {
            c._receipt(state, "resume", recovery.reasonCode, recovery.policyVersion, 0)
            state.meta.fault = null; state.meta.retry = { attempt: 0, notBefore: 0 }
            return outcome("resumed")
          }
        } else if (state.meta.fault) return outcome("paused", "deliveryFaultPaused")
        if (state.meta.retry.notBefore > c._now()) return outcome(recovery ? "recoveryPending" : "pending", "retryCooldown")
        const pending = state.records.filter(record => !record.quarantined)
        if (!pending.length) return outcome(recovery ? "recoveryPending" : "empty", recovery ? "probeEventRequired" : "empty")
        const maximum = recovery ? 1 : Math.min(c.cfg.maxEventsPerRequest, state.meta.adaptiveMaxEvents)
        const records = []
        let bytes = byteLength('{"events":[]}')
        for (const record of pending.slice(0, maximum)) {
          if (!validateFrozen(record.payloadJson, c.cfg)) {
            c._latch(state, "invalidFrozenEvent", 0, [record.handle])
            return outcome(recovery ? "recoveryPending" : "paused", "invalidFrozenEvent")
          }
          const addition = byteLength(record.payloadJson) + (records.length ? 1 : 0)
          if (bytes + addition > c.cfg.maxRequestBytes) break
          bytes += addition; records.push(record)
        }
        if (!records.length) { c._latch(state, "singleEventTooLarge", 0, [pending[0].handle]); return outcome("paused", "singleEventTooLarge") }
        const attempt = state.meta.retry.attempt >= c.cfg.maxRetries + 1 ? 1 : state.meta.retry.attempt + 1
        const claim = {
          token: uuid(c.runtime.crypto), storeInstanceId: state.meta.storeInstanceId,
          permissionEpoch: state.meta.permissionEpoch, endpointRevision: state.meta.endpointRevision,
          handles: records.map(record => record.handle), attempt, probe: Boolean(recovery),
          faultRevision: state.meta.fault?.revision ?? 0, queueRevision: state.meta.queueRevision,
        }
        state.meta.claim = claim
        state.meta.retry = { attempt, notBefore: c._now() + c.cfg.timeoutMs + this._delay(attempt) }
        batch = { claim, records, capability, recovery, payload: serializeBatch(records) }
        return null
      })
      if (early) return early
      if (signal?.aborted || !c._localValid(capability) || c.store.faulted || c._suspended || c._hidden ||
          batch.records.some(record => record.enqueuedAt <= c._now() - c.cfg.retentionDays * 86400000)) return outcome("pending", "stalePreparation")
      const controller = new AbortController()
      const abort = () => controller.abort()
      signal?.addEventListener("abort", abort, { once: true })
      c._abortController = controller
      // The vendored primitive invokes fetch BEFORE its first await. Return an
      // object containing the promise, not the promise, to release the state
      // gate at request start while sender ownership spans durable completion.
      const request = performCollectorRequest({
        collectorUrl: c.cfg.collectorUrl, apiKey: c.cfg.apiKey, payload: batch.payload,
        fetchImpl: c.runtime.fetch, signal: controller.signal, timeoutMs: c.cfg.timeoutMs,
        timeoutPolicy: "body", maxResponseBytes: c.cfg.maxResponseBytes,
        keepalive: false, redirect: "error", credentials: "omit", referrerPolicy: "no-referrer",
      })
      request.catch(() => {})
      return { ...batch, request, controller, detachAbort: () => signal?.removeEventListener("abort", abort) }
      })
    })
  }

  async _settle(started) {
    const c = this.client
    let response
    try { response = await started.request } catch {
      response = { ok: false, status: 0, networkError: true, cancelled: started.controller.signal.aborted }
    } finally { started.detachAbort?.(); if (c._abortController === started.controller) c._abortController = null }
    if (!c._localValid(started.capability) || response.cancelled) return outcome(started.recovery ? "recoveryPending" : "paused", "requestCancelled")
    return c.coord.state(() => c.store.update(state => {
      c._require(state, started.capability)
      const claim = state.meta.claim
      if (!claim || claim.token !== started.claim.token || !c._matches(state.meta, started.capability)) return outcome("pending", "staleCompletion")
      state.meta.claim = null
      state.meta.lastHttpStatus = response.status
      const records = started.records.filter(prepared => state.records.some(record => record.handle === prepared.handle && record.payloadJson === prepared.payloadJson && record.enqueuedAt === prepared.enqueuedAt && !record.quarantined))
      if (started.recovery && (!state.meta.fault || state.meta.fault.revision !== claim.faultRevision || state.meta.queueRevision !== claim.queueRevision)) return outcome("recoveryPending", "staleRecovery")
      if (response.ok && !response.networkError && !response.timedOut) {
        if (Number.isSafeInteger(response.body?.accepted_events) && response.body.accepted_events === started.records.length && !response.bodyError) {
          if (started.recovery && records.length !== 1) return outcome("recoveryPending", "probeRecordChanged")
          c._remove(state, records.map(record => record.handle))
          state.meta.retry = { attempt: 0, notBefore: 0 }
          if (started.recovery) {
            c._receipt(state, "validatedResume", started.recovery.reasonCode, started.recovery.policyVersion, records.length)
            state.meta.fault = null
          }
          return { status: started.recovery ? "resumed" : "sent", safeDiagnosticCode: "collectorAccepted", acceptedCount: started.records.length }
        }
        if (!started.recovery) c._latch(state, "acceptedCountMismatch", response.status)
        return outcome(started.recovery ? "recoveryPending" : "paused", "acceptedCountMismatch")
      }
      const transient = response.networkError || response.timedOut || response.status === 408 || response.status === 429 || response.status >= 500
      if (transient) {
        state.meta.retry.notBefore = c._now() + Math.max(this._delay(claim.attempt), retryAfterMs(response.retryAfter, c._now()))
        return outcome(started.recovery ? "recoveryPending" : "pending", "retryScheduled")
      }
      if (response.status === 413 && started.records.length > 1 && !started.recovery) {
        state.meta.adaptiveMaxEvents = Math.max(1, Math.floor(started.records.length / 2))
        state.meta.retry = { attempt: 0, notBefore: 0 }
        return outcome("sent", "batchReduced")
      }
      if ([400, 413, 422].includes(response.status)) {
        c._latch(state, `collectorHttp${response.status}`, response.status, records.map(record => record.handle))
      } else if (!started.recovery) c._latch(state, "collectorAuthOrUnexpectedResponse", response.status)
      return outcome(started.recovery ? "recoveryPending" : "paused", "deliveryFaultPaused")
    }))
  }

  async recover(options = {}) {
    const c = this.client
    if (c._inHook) return outcome("invalidEvent", "callbackReentrancy")
    try { options = snapshotRecoveryOptions(options) } catch { return outcome("invalidEvent", "invalidRecoveryPolicy") }
    if (!Number.isSafeInteger(options.expectedFaultRevision) || options.expectedFaultRevision < 1 ||
        !Number.isSafeInteger(options.expectedQueueRevision) || options.expectedQueueRevision < 0 ||
        !["configurationCorrected", "collectorLimitCorrected", "sdkFixValidated", "postDisposalValidation"].includes(options.reasonCode) ||
        typeof options.policyVersion !== "string" || !/^[a-zA-Z0-9_.-]{1,64}$/.test(options.policyVersion)) return outcome("invalidEvent", "invalidRecoveryPolicy")
    const capability = c._capability
    try {
      const response = await c.coord.sender(async () => {
        c._senderActive = true; c._lastBusy = false
        try {
          const started = await this._start(capability, options)
          return started.request ? await this._settle(started) : started
        } finally { c._senderActive = false }
      })
      if (response.status === "resumed") c._wake()
      return response
    } catch (error) { return c._outcome(error, "recoveryPending") }
  }
}
