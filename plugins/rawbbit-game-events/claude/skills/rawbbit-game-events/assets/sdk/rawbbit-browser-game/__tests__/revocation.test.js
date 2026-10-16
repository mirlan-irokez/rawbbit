import test from "node:test"
import assert from "node:assert/strict"
import { webcrypto } from "node:crypto"
import { createLocalStorageRevocationStore } from "../index.js"

class MemoryStorage {
  values = new Map()
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
}

test("local revocation journal persists only scoped opaque tokens and clears by compare-and-swap", async () => {
  const storage = new MemoryStorage()
  const first = createLocalStorageRevocationStore({ appId: "game-a", environment: "prod", storage, crypto: webcrypto })
  const afterRestart = createLocalStorageRevocationStore({ appId: "game-a", environment: "prod", storage, crypto: webcrypto })
  const otherApp = createLocalStorageRevocationStore({ appId: "game-b", environment: "prod", storage, crypto: webcrypto })
  const otherEnvironment = createLocalStorageRevocationStore({ appId: "game-a", environment: "test", storage, crypto: webcrypto })

  assert.equal(await first.readPending(), null)
  const original = await first.markPending()
  assert.match(original, /^[0-9a-f-]{36}$/i)
  assert.equal(await afterRestart.readPending(), original)
  assert.equal(await otherApp.readPending(), null)
  assert.equal(await otherEnvironment.readPending(), null)

  const newer = await afterRestart.markPending()
  assert.notEqual(newer, original)
  assert.equal(await first.clearPending(original), false)
  assert.equal(await afterRestart.readPending(), newer)
  assert.equal(await afterRestart.clearPending(newer), true)
  assert.equal(await first.readPending(), null)
  assert.equal([...storage.values.values()].some(value => value.includes("game-a") || value.includes("prod")), false)
})

test("malformed journal values and unavailable storage fail closed", async () => {
  const storage = new MemoryStorage()
  const journal = createLocalStorageRevocationStore({ appId: "game", environment: "prod", storage, crypto: webcrypto })
  const key = [...storage.values.keys()][0] ?? "rawbbit-browser-game:pending-revocation:game:prod"
  storage.setItem(key, "not a bounded opaque token with spaces")
  await assert.rejects(journal.readPending(), error => error.code === "revocationStorageInvalid")
  await assert.rejects(journal.clearPending("bad token"), error => error.code === "revocationStorageInvalid")

  const denied = {
    getItem() { throw new Error("secret storage diagnostic") },
    setItem() { throw new Error("secret storage diagnostic") },
    removeItem() { throw new Error("secret storage diagnostic") },
  }
  const unavailable = createLocalStorageRevocationStore({ appId: "game", environment: "prod", storage: denied, crypto: webcrypto })
  for (const operation of [() => unavailable.readPending(), () => unavailable.markPending(), () => unavailable.clearPending("token")]) {
    await assert.rejects(operation(), error => {
      assert.equal(error.code, "revocationStorageFailure")
      assert.equal(error.message.includes("secret storage diagnostic"), false)
      return true
    })
  }
})
