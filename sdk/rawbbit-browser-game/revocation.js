import { BrowserGameError } from "./coordinator.js"
import { headerSafeKey, uuid } from "./payload.js"

const validToken = value => typeof value === "string" && value.length > 0 && value.length <= 128 &&
  /^[a-zA-Z0-9_.:-]+$/.test(value)

/**
 * A minimal origin-local revocation journal for browser hosts. SDK instances
 * serialize its operations with the namespace's Web Lock. The value contains
 * only a random marker; never event, identity, endpoint, or key data.
 *
 * Hosts may supply a stronger durable store through the same interface.
 */
export function createLocalStorageRevocationStore({
  appId,
  environment,
  storage,
  crypto: cryptoImpl = globalThis.crypto,
} = {}) {
  headerSafeKey(appId, "appId", 256)
  headerSafeKey(environment, "environment", 128)
  let target = storage
  if (target === undefined) {
    try { target = globalThis.localStorage } catch { throw new BrowserGameError("revocationStorageUnavailable") }
  }
  if (!target || typeof target.getItem !== "function" || typeof target.setItem !== "function" || typeof target.removeItem !== "function") {
    throw new BrowserGameError("revocationStorageUnavailable")
  }
  const key = `rawbbit-browser-game:pending-revocation:${encodeURIComponent(appId)}:${encodeURIComponent(environment)}`

  return Object.freeze({
    async readPending() {
      let value
      try { value = target.getItem(key) } catch { throw new BrowserGameError("revocationStorageFailure") }
      if (value === null) return null
      if (!validToken(value)) throw new BrowserGameError("revocationStorageInvalid")
      return value
    },
    async markPending() {
      const token = uuid(cryptoImpl)
      try {
        target.setItem(key, token)
        if (target.getItem(key) !== token) throw new Error("revocation marker verification failed")
      } catch { throw new BrowserGameError("revocationStorageFailure") }
      return token
    },
    async clearPending(token) {
      if (!validToken(token)) throw new BrowserGameError("revocationStorageInvalid")
      try {
        if (target.getItem(key) !== token) return false
        target.removeItem(key)
        // removeItem is synchronous. Avoid a post-delete read that could fail
        // after the durable marker is already gone and falsely report failure.
        return true
      } catch { throw new BrowserGameError("revocationStorageFailure") }
    },
  })
}
