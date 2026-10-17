import { BrowserGameStore } from "./store.js"
import { Coordinator, BrowserGameError } from "./coordinator.js"
import { normalizeConfig, uuid, byteLength, detachJson, snapshotInputs, buildEnvelope, validateFrozen, addReceipt, snapshotRecoveryOptions } from "./payload.js"
import { emptySession, sessionCandidate, eventContext } from "./session.js"
import { Sender } from "./sender.js"
import { attachLifecycle } from "./lifecycle.js"

const instances = new WeakMap()
export const increment = value => {
  if (!Number.isSafeInteger(value) || value < 0 || value === Number.MAX_SAFE_INTEGER) throw new BrowserGameError("storageInvalidState")
  return value + 1
}
const frozen = value => {
  if (value && typeof value === "object") { for (const child of Object.values(value)) frozen(child); Object.freeze(value) }
  return value
}
const result = (status, code = status) => ({ status, safeDiagnosticCode: code })
const validRevocationToken = value => typeof value === "string" && value.length > 0 && value.length <= 128 && /^[a-zA-Z0-9_.:-]+$/.test(value)

export function createBrowserGameClient(config) { return BrowserGameClient.create(config) }

export class BrowserGameClient {
  static async create(input) {
    const cfg = normalizeConfig(input)
    const win = cfg.window
    const runtime = {
      window: win, document: cfg.document ?? win?.document,
      indexedDB: cfg.indexedDB, locks: cfg.locks, crypto: cfg.crypto,
      fetch: cfg.fetchImpl, BroadcastChannel: cfg.BroadcastChannel,
      isSecureContext: cfg.secureContext,
    }
    if (!runtime.isSecureContext || !win || win.top !== win.self || !runtime.indexedDB ||
        typeof runtime.locks?.request !== "function" || typeof runtime.fetch !== "function" ||
      !(typeof runtime.crypto?.randomUUID === "function" || typeof runtime.crypto?.getRandomValues === "function") ||
      typeof AbortController !== "function" || typeof structuredClone !== "function" || typeof TextEncoder !== "function") {
      throw new BrowserGameError("unsupportedBrowser")
    }
    // Also establish secure ID capability before opening storage.
    const storeInstanceId = uuid(runtime.crypto)
    const scope = `rawbbit-browser-game:${encodeURIComponent(cfg.appId)}:${encodeURIComponent(cfg.environment)}`
    let scopes = instances.get(win)
    if (!scopes) { scopes = new Set(); instances.set(win, scopes) }
    if (scopes.has(scope)) throw new BrowserGameError("alreadyInitialized")
    scopes.add(scope)
    const client = new BrowserGameClient(cfg, runtime, scope, () => scopes.delete(scope))
    try {
      client.store = await BrowserGameStore.open({
        indexedDB: runtime.indexedDB, name: scope, timeoutMs: cfg.storageTimeoutMs,
        onFault: code => { client._storageFailure = code; client._invalidate("consentUnknown") },
        initialMeta: {
          schemaVersion: 1, storeInstanceId,
          installationId: cfg.legacyIdentityImport?.pseudoId ?? uuid(runtime.crypto),
          identityImportPolicyVersion: cfg.legacyIdentityImport?.policyVersion ?? null,
          appId: cfg.appId, environment: cfg.environment, endpoint: cfg.collectorUrl,
          permissionEpoch: 0, endpointRevision: 0, queueRevision: 0, sessionRevision: 0, sequence: 0,
          session: emptySession(), queueBytes: 0, counters: { expired: 0, evicted: 0, quarantinedExpired: 0, quarantinedEvicted: 0, discarded: 0 },
          retry: { attempt: 0, notBefore: 0 }, fault: null, claim: null, adaptiveMaxEvents: cfg.maxEventsPerRequest,
        },
      })
      const initial = await client.coord.state(() => client.store.read())
      client._assertState(initial)
      client._instance = initial.meta.storeInstanceId
      if (cfg.legacyIdentityImport && initial.meta.installationId !== cfg.legacyIdentityImport.pseudoId) throw new BrowserGameError("identityImportRequiresEmptyStore")
      if (initial.meta.endpoint !== cfg.collectorUrl) {
        await client.coord.sender(() => client.coord.revocation(() => client.coord.state(() => client.store.update(state => {
          client._assertState(state)
          if (state.meta.endpoint !== cfg.previousCollectorUrl) throw new BrowserGameError("endpointMigrationRequired")
          state.meta.endpoint = cfg.collectorUrl
          state.meta.endpointRevision = increment(state.meta.endpointRevision)
          state.meta.permissionEpoch = increment(state.meta.permissionEpoch)
          state.meta.queueRevision = increment(state.meta.queueRevision)
          state.meta.claim = null
          if (state.meta.fault?.requiresProbe) {
            state.meta.fault.probeValidated = false
            state.meta.fault.revision = increment(state.meta.fault.revision)
          }
          client._receipt(state, "endpointMigration", "configurationCorrected", "sdk_v1", 0)
        }))))
      }
      await client.coord.state(() => client.store.update(state => { client._assertState(state); client._prune(state) }))
      client.sender = new Sender(client)
      client._detachLifecycle = attachLifecycle(client, runtime)
      // Migration is a shared revocation boundary; notifications are advisory.
      if (initial.meta.endpoint !== cfg.collectorUrl) client._notify("revoke")
      return client
    } catch (error) {
      client.dispose()
      throw error instanceof BrowserGameError ? error : new BrowserGameError(error.code || "storageFailure")
    }
  }

  constructor(cfg, runtime, scope, unregister) {
    this.cfg = cfg; this.runtime = runtime; this.scope = scope; this._unregister = unregister
    this.coord = new Coordinator(runtime.locks, scope, cfg.lockTimeoutMs)
    this._generation = 0; this._capability = null; this._consent = "unknown"
    this._disposed = false; this._suspended = false; this._hidden = runtime.document?.visibilityState === "hidden"
    this._pending = 0; this._pendingBytes = 0; this._inHook = false; this._pump = null; this._storageFailure = null
    this._maintenance = null; this._nextMaintenance = 0
    this._withdrawIncomplete = false
    this._revocationUncertain = false
    this._withdrawOperation = null
    this._withdrawInProgress = null
    this._senderActive = false; this._lastBusy = false
    this._notify = () => {}; this._abortController = null
  }

  get hasCurrentConsent() { return Boolean(this._capability && !this._disposed && !this._suspended && !this.store?.faulted) }
  _now() {
    const now = this.cfg.clock()
    if (!Number.isFinite(now) || now < 0) throw new BrowserGameError("invalidClock")
    return now
  }
  _abortRequest() { this._abortController?.abort() }
  _invalidate(consent = "consentUnknown") {
    this._generation += 1; this._capability = null
    this._consent = consent === "consentWithdrawn" ? "withdrawn" : "unknown"
    this._abortRequest()
  }
  _permissionRequired() {
    try { Promise.resolve(this.cfg.onPermissionRequired?.()).catch(() => {}) } catch { /* Never log host exceptions. */ }
  }
  _fence(meta) {
    return { storeInstanceId: meta.storeInstanceId, permissionEpoch: meta.permissionEpoch, endpointRevision: meta.endpointRevision, generation: this._generation }
  }
  _matches(meta, fence) {
    return Boolean(fence && fence.storeInstanceId === this._instance && meta.storeInstanceId === fence.storeInstanceId &&
      meta.permissionEpoch === fence.permissionEpoch && meta.endpointRevision === fence.endpointRevision && meta.endpoint === this.cfg.collectorUrl)
  }
  _localValid(capability) {
    return this.hasCurrentConsent && this._capability === capability && capability.generation === this._generation
  }
  _require(state, capability) {
    this._assertState(state)
    if (!this._localValid(capability) || !this._matches(state.meta, capability)) {
      this._invalidate(this._consent === "withdrawn" ? "consentWithdrawn" : "consentUnknown")
      throw new BrowserGameError("consentChanged")
    }
  }
  _assertState(state) {
    const m = state.meta
    if (this.store?.faulted) throw new BrowserGameError(this.store.failureCode)
    if (m.appId !== this.cfg.appId || m.environment !== this.cfg.environment ||
        (this._instance && m.storeInstanceId !== this._instance) ||
        !Number.isSafeInteger(m.queueBytes) || m.queueBytes < 0 ||
        !Number.isSafeInteger(m.session.counter) || m.session.counter < 0 ||
        !(m.session.id === null || typeof m.session.id === "string") ||
        !Number.isFinite(m.session.lastActivity) || m.session.lastActivity < 0 ||
        !Number.isSafeInteger(m.retry.attempt) || m.retry.attempt < 0 || !Number.isFinite(m.retry.notBefore) || m.retry.notBefore < 0 ||
        ["expired", "evicted", "quarantinedExpired", "quarantinedEvicted", "discarded"].some(key => !Number.isSafeInteger(m.counters[key]) || m.counters[key] < 0) ||
        !Number.isSafeInteger(m.adaptiveMaxEvents) || m.adaptiveMaxEvents < 1 || m.adaptiveMaxEvents > 500 ||
        (m.receiptSequence !== undefined && (!Number.isSafeInteger(m.receiptSequence) || m.receiptSequence < 0)) ||
        state.receipts.some(receipt => receipt.order !== undefined && (!Number.isSafeInteger(receipt.order) || receipt.order < 1 || receipt.order > (m.receiptSequence ?? 0))) ||
        (m.fault !== null && (typeof m.fault.code !== "string" || !/^[a-zA-Z0-9]{1,64}$/.test(m.fault.code) ||
          !Number.isSafeInteger(m.fault.revision) || m.fault.revision < 1 || typeof m.fault.requiresProbe !== "boolean" || typeof m.fault.probeValidated !== "boolean")) ||
        (m.claim !== null && (typeof m.claim.token !== "string" || !Array.isArray(m.claim.handles) || m.claim.handles.length > 500 ||
          m.claim.handles.some(handle => typeof handle !== "string") || !Number.isSafeInteger(m.claim.attempt) || m.claim.attempt < 1)) ||
        state.records.reduce((sum, record) => sum + record.bytes, 0) !== m.queueBytes ||
        state.records.some(record => record.bytes !== byteLength(record.payloadJson) + 512 || record.sequence > m.sequence) ||
        new Set(state.records.map(record => record.sequence)).size !== state.records.length) {
      this._storageFailure = "storageInvalidState"
      this._invalidate()
      throw new BrowserGameError("storageInvalidState")
    }
  }
  _remove(state, handles) {
    const wanted = new Set(handles)
    const removed = state.records.filter(record => wanted.has(record.handle))
    state.records = state.records.filter(record => !wanted.has(record.handle))
    if (removed.length) {
      state.meta.queueBytes -= removed.reduce((sum, record) => sum + record.bytes, 0)
      state.meta.queueRevision = increment(state.meta.queueRevision)
    }
    return removed
  }
  _prune(state) {
    const cutoff = this._now() - this.cfg.retentionDays * 86400000
    const removed = this._remove(state, state.records.filter(record => record.enqueuedAt <= cutoff).map(record => record.handle))
    for (const record of removed) {
      state.meta.counters.expired = increment(state.meta.counters.expired)
      if (record.quarantined) state.meta.counters.quarantinedExpired = increment(state.meta.counters.quarantinedExpired)
    }
  }
  _receipt(state, operation, reason, policy, count) {
    addReceipt(state, operation, reason, policy, count, this._now(), this.runtime.crypto)
  }
  _latch(state, code, status = 0, handles = []) {
    const wanted = new Set(handles)
    for (const record of state.records) if (wanted.has(record.handle)) {
      record.quarantined = true; record.httpStatus = status; record.faultCode = code
    }
    state.meta.fault = { code, httpStatus: status, revision: increment(state.meta.fault?.revision ?? 0), requiresProbe: true, probeValidated: false }
    state.meta.retry = { attempt: 0, notBefore: 0 }
    state.meta.queueRevision = increment(state.meta.queueRevision)
  }
  _outcome(error, fallback = "storageFailure") {
    if (error.code === "busy") { this._lastBusy = true; return result("busy") }
    if (["revocationStorageFailure", "revocationStorageInvalid"].includes(error.code)) return result("consentUnknown", error.code)
    if (error.code === "withdrawalIncomplete") return result("consentUnknown", "withdrawalIncomplete")
    if (["consentChanged", "disposed"].includes(error.code)) return result(this._consent === "withdrawn" ? "consentWithdrawn" : "consentUnknown", error.code)
    return result(fallback, fallback)
  }

  async _readPendingRevocation() {
    let token
    try { token = await this.cfg.revocationStore.readPending() } catch {
      this._revocationUncertain = true
      this._withdrawIncomplete = true
      this._revocationFailure = "revocationStorageFailure"
      this._invalidate("consentUnknown")
      throw new BrowserGameError("revocationStorageFailure")
    }
    if (token !== null && !validRevocationToken(token)) {
      this._revocationUncertain = true
      this._withdrawIncomplete = true
      this._revocationFailure = "revocationStorageInvalid"
      this._invalidate("consentUnknown")
      throw new BrowserGameError("revocationStorageInvalid")
    }
    // A null read cannot clear an active or uncertain local withdrawal. Only
    // the matching successful purge-and-clear path may release that latch.
    if (token !== null || this._revocationUncertain) this._withdrawIncomplete = true
    else if (this._withdrawInProgress === null) this._withdrawIncomplete = false
    return token
  }

  async _markPendingRevocation(operation) {
    let previous
    try { previous = await this._readPendingRevocation() }
    catch (error) {
      // Explicit withdrawal is also the recovery path for a malformed or
      // unreadable old marker. markPending must durably replace it, and the
      // fresh value is still verified before any queue purge begins.
      if (!["revocationStorageFailure", "revocationStorageInvalid"].includes(error.code)) throw error
      previous = undefined
    }
    let token
    try { token = await this.cfg.revocationStore.markPending() } catch {
      if (this._withdrawOperation === operation) {
        this._revocationUncertain = true
        this._withdrawIncomplete = true
        this._revocationFailure = "revocationStorageFailure"
      }
      throw new BrowserGameError("revocationStorageFailure")
    }
    if (!validRevocationToken(token) || token === previous) {
      if (this._withdrawOperation === operation) {
        this._revocationUncertain = true
        this._withdrawIncomplete = true
        this._revocationFailure = "revocationStorageInvalid"
      }
      throw new BrowserGameError("revocationStorageInvalid")
    }
    const persisted = await this._readPendingRevocation()
    if (persisted !== token) {
      if (this._withdrawOperation === operation) {
        this._revocationUncertain = true
        this._withdrawIncomplete = true
        this._revocationFailure = "revocationStorageFailure"
      }
      throw new BrowserGameError("revocationStorageFailure")
    }
    if (this._withdrawOperation === operation) {
      this._revocationUncertain = false
      this._withdrawIncomplete = true
      this._revocationFailure = null
    }
    return token
  }

  async _clearPendingRevocation(token, operation) {
    let cleared
    try { cleared = await this.cfg.revocationStore.clearPending(token) } catch {
      if (this._withdrawOperation === operation) {
        this._revocationUncertain = true
        this._withdrawIncomplete = true
        this._revocationFailure = "revocationStorageFailure"
      }
      throw new BrowserGameError("revocationStorageFailure")
    }
    if (cleared !== true) {
      if (this._withdrawOperation === operation) {
        this._revocationUncertain = true
        this._withdrawIncomplete = true
        this._revocationFailure = "revocationClearPending"
      }
      return false
    }
    if (this._withdrawOperation === operation) {
      this._revocationUncertain = false
      this._revocationFailure = null
    }
    return true
  }

  async confirmConsent(permissionProvider) {
    if (this._inHook) return result("invalidEvent", "callbackReentrancy")
    this._invalidate()
    const generation = this._generation
    if (this._disposed || this._suspended || typeof permissionProvider !== "function") return result("consentUnknown", "permissionCheckFailed")
    try {
      const pendingRevocation = await this.coord.revocation(() => this._readPendingRevocation())
      if (this._withdrawIncomplete || pendingRevocation !== null) return result("consentUnknown", "withdrawalIncomplete")
      const captured = await this.coord.state(async () => {
        const state = await this.store.read(); this._assertState(state)
        if (state.meta.endpoint !== this.cfg.collectorUrl) throw new BrowserGameError("consentChanged")
        return this._fence(state.meta)
      })
      const allowed = await permissionProvider()
      if (generation !== this._generation || this._disposed || this._suspended) return result("consentUnknown", "consentChanged")
      if (typeof allowed !== "boolean") return result("consentUnknown", "permissionCheckFailed")
      if (!allowed) return this._withdraw(captured)
      const granted = await this.coord.revocation(async () => {
        const pending = await this._readPendingRevocation()
        if (this._withdrawIncomplete || pending !== null) return false
        return this.coord.state(async () => {
          const state = await this.store.read(); this._assertState(state)
          if (generation !== this._generation || !this._matches(state.meta, captured) || this._disposed || this._suspended) return false
          this._capability = Object.freeze(captured); this._consent = "granted"
          return true
        })
      })
      if (!granted) return result("consentUnknown", this._withdrawIncomplete ? "withdrawalIncomplete" : "consentChanged")
      this._wake()
      return { status: "confirmed", safeDiagnosticCode: "consentConfirmed" }
    } catch (error) { return this._outcome(error, "consentUnknown") }
  }

  withdrawConsent() {
    if (this._inHook) return Promise.resolve(result("invalidEvent", "callbackReentrancy"))
    return this._withdraw()
  }
  async _withdraw(expected) {
    const previousIncomplete = this._withdrawIncomplete
    const previousUncertain = this._revocationUncertain
    const previousFailure = this._revocationFailure
    const operation = Symbol("withdrawal")
    this._withdrawOperation = operation
    this._withdrawInProgress = operation
    this._invalidate("consentWithdrawn")
    const generation = this._generation
    this._withdrawIncomplete = true
    if (!expected) this._notify("revoke")
    let revocationToken
    let revocationCleared = false
    let removed = 0
    try {
      await this.coord.revocation(async () => {
        if (expected) {
          await this.coord.state(async () => {
            const state = await this.store.read(); this._assertState(state)
            if (generation !== this._generation || !this._matches(state.meta, expected)) {
              throw new BrowserGameError("consentChanged")
            }
          })
          this._notify("revoke")
        }
        if (expected && generation !== this._generation) throw new BrowserGameError("consentChanged")
        revocationToken = await this._markPendingRevocation(operation)

        // Keep this revocation lock through the durable purge and marker clear.
        // Consent grants, sends and endpoint migration use the same barrier, so
        // no stale denial can strand its marker across a namespace migration.
        removed = await this.coord.state(() => this.store.update(state => {
          this._assertState(state)
          if (this._withdrawOperation !== operation ||
              (expected && (generation !== this._generation || !this._matches(state.meta, expected)))) {
            throw new BrowserGameError("consentChanged")
          }
          const count = state.records.length
          state.meta.permissionEpoch = increment(state.meta.permissionEpoch)
          state.meta.sessionRevision = increment(state.meta.sessionRevision)
          state.meta.session = emptySession(); state.records = []; state.meta.queueBytes = 0
          state.meta.queueRevision = increment(state.meta.queueRevision)
          state.meta.claim = null; state.meta.retry = { attempt: 0, notBefore: 0 }
          this._receipt(state, "consentWithdrawal", "consentWithdrawn", "sdk_v1", count)
          return count
        }))
        this._notify("revoke")
        try { revocationCleared = await this._clearPendingRevocation(revocationToken, operation) }
        catch { revocationCleared = false }
      })
    } catch (error) {
      if (expected && error.code === "consentChanged" && !revocationToken && this._withdrawOperation === operation) {
        this._withdrawIncomplete = previousIncomplete
        this._revocationUncertain = previousUncertain
        this._revocationFailure = previousFailure
        if (generation === this._generation) this._consent = "unknown"
      } else if (this._withdrawOperation === operation) {
        this._withdrawIncomplete = true
        // No verified marker means a later null read must not release this
        // local fail-closed latch after a lock timeout or uncertain write.
        if (!revocationToken) this._revocationUncertain = true
      }
      if (this._withdrawInProgress === operation) this._withdrawInProgress = null
      return { ...this._outcome(error), globalWithdrawalCommitted: false, revocationCleared: false, localDisabled: true }
    }
    if (this._withdrawOperation === operation) this._withdrawIncomplete = !revocationCleared
    if (this._withdrawInProgress === operation) this._withdrawInProgress = null
    return { status: "withdrawn", safeDiagnosticCode: revocationCleared ? "withdrawalCommitted" : "revocationClearPending",
      removed, globalWithdrawalCommitted: true, revocationCleared, localDisabled: this._withdrawIncomplete }
  }

  track(input) { return this.trackBatch([input]) }
  trackBatch(inputs) {
    if (this._inHook) return Promise.resolve(result("invalidEvent", "callbackReentrancy"))
    if (!this.hasCurrentConsent) return Promise.resolve(result(this._consent === "withdrawn" ? "consentWithdrawn" : "consentUnknown"))
    if (this._pending >= this.cfg.maxPendingTrackOperations) return Promise.resolve(result("busy"))
    const capability = this._capability
    let snapshot, now
    try { now = this._now(); snapshot = snapshotInputs(inputs, this.cfg, now) } catch { return Promise.resolve(result("invalidEvent")) }
    if (snapshot.bytes + this._pendingBytes > this.cfg.maxPendingTrackBytes) return Promise.resolve(result("busy"))
    const reservation = { bytes: snapshot.bytes }
    this._pending++; this._pendingBytes += reservation.bytes
    return this._track(snapshot, now, capability, reservation).finally(() => { this._pending--; this._pendingBytes -= reservation.bytes })
  }
  _hook(callback, ...args) {
    this._inHook = true
    try {
      const value = callback(...args.map(frozen))
      if (value && typeof value.then === "function") { Promise.resolve(value).catch(() => {}); throw new BrowserGameError("invalidCallback") }
      return value
    } finally { this._inHook = false }
  }
  _mapped(input, context) {
    if (!this.cfg.eventContextMapper) return input
    const mapping = this._hook(this.cfg.eventContextMapper, context, detachJson(input, this.cfg.maxSnapshotDepth, this.cfg.maxPendingTrackBytes))
    if (mapping == null) return input
    const clone = detachJson(mapping, this.cfg.maxSnapshotDepth)
    if (!clone || typeof clone !== "object" || Array.isArray(clone)) throw new BrowserGameError("invalidCallback")
    const allowed = { eventParams: "event_params", userProperties: "user_properties", trafficSource: "traffic_source", geo: null, device: null }
    const output = detachJson(input, this.cfg.maxSnapshotDepth, this.cfg.maxPendingTrackBytes)
    for (const [key, value] of Object.entries(clone)) {
      const canonical = Object.keys(allowed).find(name => name === key || allowed[name] === key)
      if (!canonical || (canonical === "device" && Object.hasOwn(value, "platform"))) throw new BrowserGameError("invalidCallback")
      delete output[canonical]; if (allowed[canonical]) delete output[allowed[canonical]]
      output[canonical] = value
    }
    return output
  }
  async _track(snapshot, now, capability, reservation) {
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        const candidateState = await this.coord.state(async () => { const state = await this.store.read(); this._require(state, capability); return state })
        const candidate = sessionCandidate(candidateState.meta.session, now, this.cfg.sessionTimeoutMs, () => uuid(this.runtime.crypto))
        let prefix = null
        if (candidate.isNewSession && this.cfg.sessionStartFactory) {
          const input = this._hook(this.cfg.sessionStartFactory, eventContext(candidate, 1, snapshot.inputs[0].eventTimestamp, true), detachJson(snapshot.inputs[0], this.cfg.maxSnapshotDepth, this.cfg.maxPendingTrackBytes))
          if (input != null) {
            prefix = detachJson(snapshotInputs([input], this.cfg, now).inputs[0], this.cfg.maxSnapshotDepth, this.cfg.maxPendingTrackBytes)
            prefix.eventTimestamp = snapshot.inputs[0].eventTimestamp
          }
        }
        let counter = candidate.counter
        const prepared = []
        let preparedBytes = 0
        const prepare = async input => {
          const envelope = await buildEnvelope(input, this.cfg, { installationId: candidateState.meta.installationId, sessionId: candidate.id })
          const payloadJson = JSON.stringify(envelope)
          if (!validateFrozen(payloadJson, this.cfg)) throw new BrowserGameError("invalidEvent")
          const bytes = byteLength(payloadJson) + 512
          preparedBytes += bytes
          const required = Math.max(snapshot.bytes, preparedBytes)
          if (this._pendingBytes + required - reservation.bytes > this.cfg.maxPendingTrackBytes) throw new BrowserGameError("busy")
          this._pendingBytes += required - reservation.bytes; reservation.bytes = required
          prepared.push({ handle: uuid(this.runtime.crypto), payloadJson, bytes, quarantined: false })
        }
        if (prefix) { counter = increment(counter); await prepare(prefix) }
        for (const input of snapshot.inputs) {
          counter = increment(counter)
          await prepare(this._mapped(input, eventContext(candidate, counter, input.eventTimestamp, Boolean(prefix))))
        }
        const bytes = prepared.reduce((sum, record) => sum + record.bytes, 0)
        if (bytes > this.cfg.maxQueueBytes || bytes > this.cfg.maxPendingTrackBytes) return result("invalidEvent", "operationTooLarge")
        const committed = await this.coord.revocation(async () => {
          const pending = await this._readPendingRevocation()
          if (this._withdrawIncomplete || pending !== null) {
            this._invalidate("consentUnknown")
            throw new BrowserGameError("withdrawalIncomplete")
          }
          return this.coord.state(() => this.store.update(state => {
            this._require(state, capability)
            if (state.meta.sessionRevision !== candidateState.meta.sessionRevision) return false
            this._prune(state)
            while (state.meta.queueBytes + bytes > this.cfg.maxQueueBytes) {
              const record = state.records[0]
              this._remove(state, [record.handle])
              state.meta.counters.evicted = increment(state.meta.counters.evicted)
              if (record.quarantined) state.meta.counters.quarantinedEvicted = increment(state.meta.counters.quarantinedEvicted)
            }
            for (const record of prepared) {
              state.meta.sequence = increment(state.meta.sequence)
              state.records.push({ ...record, sequence: state.meta.sequence, enqueuedAt: this._now() })
            }
            state.meta.queueBytes += bytes
            state.meta.queueRevision = increment(state.meta.queueRevision)
            state.meta.sessionRevision = increment(state.meta.sessionRevision)
            state.meta.session = { id: candidate.id, counter, lastActivity: candidate.lastActivity }
            return true
          }))
        })
        if (committed) {
          this._notify("wake"); this._wake()
          return { status: "durablyStored", safeDiagnosticCode: "durablyStored", eventIds: snapshot.inputs.map(input => input.eventId), count: snapshot.inputs.length, prefixEventId: prefix?.eventId }
        }
      }
      return result("busy", "sessionRevisionChanged")
    } catch (error) {
      if (["invalidEvent", "invalidCallback"].includes(error.code) || !(error.code)) return result("invalidEvent")
      return this._outcome(error)
    }
  }

  _wake() {
    if (!this.cfg.autoFlush || !this.hasCurrentConsent || this._hidden || this._suspended || this._pump) return
    this._pump = Promise.resolve().then(() => this.sender.attempt()).then(value => {
      if (value.status === "sent") { const timer = setTimeout(() => this._wake(), 0); timer.unref?.() }
    }).catch(() => {}).finally(() => { this._pump = null })
  }
  _maintain() {
    if (this._disposed || this._hidden || this._suspended || this._maintenance || Date.now() < this._nextMaintenance) return
    this._nextMaintenance = Date.now() + 60000
    this._maintenance = this.coord.state(() => this.store.update(state => { this._assertState(state); this._prune(state) }))
      .catch(() => {}).finally(() => { this._maintenance = null })
  }
  async flush({ timeoutMs = this.cfg.flushTimeoutMs } = {}) {
    if (this._inHook) return result("invalidEvent", "callbackReentrancy")
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 60000) return result("invalidEvent")
    const controller = new AbortController()
    let timer
    const deadline = new Promise(resolve => {
      timer = setTimeout(() => { controller.abort(); resolve(result("timeout", "flushDeadline")) }, timeoutMs)
    })
    const work = (async () => {
      while (!controller.signal.aborted) {
        const state = await this.sender.attempt(controller.signal)
        if (state.status !== "sent") return state
      }
      return result("timeout", "flushDeadline")
    })()
    // The caller is bounded; the sender lock is NOT released by this race.
    // An abort-ignoring injected transport retains ownership until settlement.
    try { return await Promise.race([work, deadline]) } finally { clearTimeout(timer) }
  }
  async getDiagnostics() {
    let state
    try { state = await this.coord.state(async () => { const value = await this.store.read(); this._assertState(value); return value }) } catch { /* No raw error leaves diagnostics. */ }
    try {
      const pending = await this.coord.revocation(() => this._readPendingRevocation())
      if (pending !== null) this._invalidate("consentUnknown")
    } catch (error) {
      if (["revocationStorageFailure", "revocationStorageInvalid"].includes(error.code)) {
        this._revocationFailure = error.code
      }
      if (error.code === "busy") this._lastBusy = true
      this._invalidate("consentUnknown")
    }
    if (state && this._capability && !this._matches(state.meta, this._capability)) this._invalidate()
    return {
      consent: this.hasCurrentConsent ? "granted" : this._consent, storageAvailable: Boolean(state && !this.store?.faulted && !this._storageFailure),
      storageCode: this._storageFailure, pendingOperations: this._pending, pendingBytes: this._pendingBytes,
      senderActive: this._senderActive, lastBusy: this._lastBusy, withdrawalIncomplete: this._withdrawIncomplete,
      revocationStorageCode: this._revocationFailure ?? null,
      pendingCount: state?.records.filter(record => !record.quarantined).length ?? 0,
      quarantinedCount: state?.records.filter(record => record.quarantined).length ?? 0,
      queueBytes: state?.meta.queueBytes ?? 0, queueRevision: state?.meta.queueRevision ?? 0,
      counters: state ? { ...state.meta.counters } : null,
      faultCode: state?.meta.fault?.code ?? null, faultRevision: state?.meta.fault?.revision ?? 0,
      recoveryProbeRequired: state?.meta.fault?.requiresProbe ?? false, recoveryProbeValidated: state?.meta.fault?.probeValidated ?? false,
      retryNotBefore: state?.meta.retry.notBefore ?? 0, retryAttempt: state?.meta.retry.attempt ?? 0,
      lastHttpStatus: state?.meta.lastHttpStatus ?? null,
    }
  }
  async listQuarantined() {
    const state = await this.coord.state(() => this.store.read())
    this._assertState(state)
    return state.records.filter(record => record.quarantined).map(record => ({ handle: record.handle, httpStatus: record.httpStatus ?? 0, faultCode: record.faultCode ?? "", enqueuedAt: record.enqueuedAt }))
  }
  retryQuarantined(handles, options) { return this._recoverRecords("retry", handles, options) }
  discardQuarantined(handles, options) { return this._recoverRecords("discard", handles, options) }
  async _recoverRecords(operation, handles, options = {}) {
    if (this._inHook) return result("invalidEvent", "callbackReentrancy")
    const capability = this._capability
    const reasons = operation === "retry" ? ["configurationCorrected", "collectorLimitCorrected", "sdkFixValidated"] : ["invalidFrozenEvent", "collectorContractRejected", "unsupportedFrozenPayload"]
    try {
      handles = Object.freeze(detachJson(handles, 2, 100000))
      options = snapshotRecoveryOptions(options)
      if (!Array.isArray(handles) || !handles.length || handles.length > 500 || new Set(handles).size !== handles.length ||
          handles.some(handle => typeof handle !== "string" || !handle.length || handle.length > 128) ||
          !reasons.includes(options.reasonCode)) throw new TypeError("Invalid recovery selection")
    } catch { return result("invalidEvent", "invalidRecoverySelection") }
    try {
      const affected = await this.coord.state(() => this.store.update(state => {
        this._require(state, capability)
        if (!state.meta.fault || state.meta.fault.revision !== options.expectedFaultRevision || state.meta.queueRevision !== options.expectedQueueRevision) throw new BrowserGameError("staleRecovery")
        const selected = handles.map(handle => state.records.find(record => record.handle === handle))
        if (selected.some(record => !record?.quarantined)) throw new BrowserGameError("invalidSelection")
        if (operation === "retry") {
          if (selected.some(record => !validateFrozen(record.payloadJson, this.cfg))) throw new BrowserGameError("invalidFrozenEvent")
          for (const record of selected) { record.quarantined = false; delete record.faultCode; delete record.httpStatus }
          state.meta.queueRevision = increment(state.meta.queueRevision)
        } else {
          this._remove(state, handles)
          for (const record of selected) state.meta.counters.discarded = increment(state.meta.counters.discarded)
        }
        state.meta.fault.revision = increment(state.meta.fault.revision)
        state.meta.fault.probeValidated = false
        state.meta.retry = { attempt: 0, notBefore: 0 }
        this._receipt(state, operation, options.reasonCode, options.policyVersion, selected.length)
        return selected.length
      }))
      return { status: "recovered", safeDiagnosticCode: "selectionCommitted", affected }
    } catch (error) { return this._outcome(error, "recoveryPending") }
  }
  validateAndResume(options = {}) { return this.sender.recover(options) }
  dispose() {
    if (this._inHook) return
    if (this._disposed) return
    this._disposed = true; this._invalidate(); this._detachLifecycle?.(); this.coord.dispose(); this.store?.close(); this._unregister?.()
  }
}
