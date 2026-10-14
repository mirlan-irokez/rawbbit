function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function shouldRetryStatus(status) {
  return status === 429 || status >= 500
}

function resolveFetch(fetchImpl) {
  if (fetchImpl) return fetchImpl
  if (typeof fetch === "function") return fetch
  throw new Error("No fetch implementation available")
}

export function resolveCollectorEndpoint(collectorUrl) {
  const trimmed = String(collectorUrl || "").trim()
  if (!trimmed) throw new Error("collectorUrl is required")
  const withoutTrailingSlash = trimmed.replace(/\/$/, "")
  if (/\/v1\/events:batch$/i.test(withoutTrailingSlash)) {
    return withoutTrailingSlash
  }
  return `${withoutTrailingSlash}/v1/events:batch`
}

function readRetryAfter(response) {
  try {
    const value = response.headers?.get("Retry-After")
    return typeof value === "string" ? value : null
  } catch {
    return null
  }
}

async function readResponseBody(response, maxResponseBytes, signal) {
  let reader
  let onAbort
  let readerCancelled = false
  const cancelReader = () => {
    if (!reader || readerCancelled) return
    readerCancelled = true
    try { Promise.resolve(reader.cancel()).catch(() => {}) } catch { /* already closed */ }
  }
  try {
    if (signal.aborted) throw signal.reason
    if (maxResponseBytes !== null) {
      // Never fall back to an unbounded json()/text() read for modern callers.
      reader = response.body.getReader()
    }
    const aborted = new Promise((_resolve, reject) => {
      onAbort = () => {
        reject(signal.reason)
        // Cancellation is best effort; a broken stream must not defeat the body deadline.
        cancelReader()
      }
      signal.addEventListener("abort", onAbort, { once: true })
    })
    const reading = (async () => {
      if (maxResponseBytes === null) return response.json()
      const chunks = []
      let size = 0
      while (true) {
        const { done, value } = await reader.read()
        if (signal.aborted) throw signal.reason
        if (done) break
        if (!(value instanceof Uint8Array)) throw new Error("Invalid response body chunk")
        size += value.byteLength
        if (size > maxResponseBytes) throw new Error("Collector response body exceeds byte limit")
        if (value.byteLength) chunks.push(value)
      }
      const bytes = new Uint8Array(size)
      let offset = 0
      for (const chunk of chunks) {
        bytes.set(chunk, offset)
        offset += chunk.byteLength
      }
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes))
    })()
    // Only body consumption races cancellation. The fetch promise itself always settles
    // before this request releases its caller's ownership, even if fetch ignores abort.
    return await Promise.race([reading, aborted])
  } catch (error) {
    cancelReader()
    throw error
  } finally {
    if (onAbort) signal.removeEventListener("abort", onAbort)
    try { reader?.releaseLock() } catch { /* an injected reader may still have a pending read */ }
  }
}

export async function performCollectorRequest({
  collectorUrl,
  apiKey,
  payload,
  fetchImpl,
  signal,
  timeoutMs = 10000,
  timeoutPolicy = "body",
  maxResponseBytes = 65536,
  keepalive = false,
  redirect = "error",
  credentials = "omit",
  referrerPolicy = "no-referrer",
}) {
  const endpoint = resolveCollectorEndpoint(collectorUrl)
  const effectiveFetch = resolveFetch(fetchImpl)
  if (typeof payload !== "string") throw new TypeError("payload must be a serialized string")
  if (timeoutPolicy !== "body" && timeoutPolicy !== "headers") {
    throw new TypeError("timeoutPolicy must be body or headers")
  }
  if (maxResponseBytes !== null && (!Number.isSafeInteger(maxResponseBytes) || maxResponseBytes < 0)) {
    throw new TypeError("maxResponseBytes must be a nonnegative safe integer or null")
  }
  if (signal?.aborted) throw signal.reason || new DOMException("Request cancelled", "AbortError")

  const controller = new AbortController()
  const onAbort = () => controller.abort(signal.reason)
  signal?.addEventListener("abort", onAbort, { once: true })
  const timeoutHandle = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const init = {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
      body: payload,
      signal: controller.signal,
      keepalive,
    }
    // null explicitly selects native fetch defaults for the compatibility wrapper.
    if (redirect !== null) init.redirect = redirect
    if (credentials !== null) init.credentials = credentials
    if (referrerPolicy !== null) init.referrerPolicy = referrerPolicy

    // No await or deferred setup may precede this invocation: the caller dispatches
    // while still holding its permission/state gate. There are no retries here.
    const response = await effectiveFetch(endpoint, init)
    if (timeoutPolicy === "headers") clearTimeout(timeoutHandle)
    const retryAfter = readRetryAfter(response)
    let body = null
    let bodyError = false
    try {
      body = maxResponseBytes === null && timeoutPolicy === "headers" && !signal
        ? await response.json()
        : await readResponseBody(response, maxResponseBytes, controller.signal)
    } catch {
      bodyError = true
    }
    return {
      ok: response.ok,
      status: response.status,
      body,
      retryAfter,
      networkError: false,
      cancelled: Boolean(signal?.aborted),
      bodyError,
      ...(timeoutPolicy === "body" && controller.signal.aborted && !signal?.aborted ? { timedOut: true } : {}),
    }
  } finally {
    clearTimeout(timeoutHandle)
    signal?.removeEventListener("abort", onAbort)
  }
}

export async function sendEventBatch({
  collectorUrl,
  apiKey,
  events,
  fetchImpl,
  timeoutMs = 5000,
  maxRetries = 2,
  backoffMs = 250,
}) {
  const endpoint = resolveCollectorEndpoint(collectorUrl)
  const effectiveFetch = resolveFetch(fetchImpl)

  const payload = JSON.stringify({ events })

  let lastError = null

  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    try {
      const response = await performCollectorRequest({
        collectorUrl: endpoint,
        apiKey,
        payload,
        fetchImpl: effectiveFetch,
        timeoutMs,
        timeoutPolicy: "headers",
        maxResponseBytes: null,
        keepalive: true,
        redirect: null,
        credentials: null,
        referrerPolicy: null,
      })

      if (!response.ok && shouldRetryStatus(response.status) && attempt < maxRetries) {
        const backoff = backoffMs * 2 ** attempt
        await sleep(backoff)
        continue
      }

      return {
        ok: response.ok,
        status: response.status,
        body: response.body,
      }
    } catch (error) {
      lastError = error
      if (attempt < maxRetries) {
        const backoff = backoffMs * 2 ** attempt
        await sleep(backoff)
        continue
      }
    }
  }

  throw lastError || new Error("Failed to send event batch")
}
