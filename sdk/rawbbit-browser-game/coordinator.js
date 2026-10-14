export class BrowserGameError extends Error {
  constructor(code) { super(code); this.name = "BrowserGameError"; this.code = code }
}

// A deadline cancels a waiting request, never steals a running Web Lock.
export class Coordinator {
  constructor(locks, scope, timeoutMs) {
    this.locks = locks
    this.scope = scope
    this.timeoutMs = timeoutMs
    this.closed = false
    this.waiters = new Set()
  }

  async state(callback) {
    if (this.closed) throw new BrowserGameError("disposed")
    const controller = new AbortController()
    this.waiters.add(controller)
    const timer = setTimeout(() => controller.abort(), this.timeoutMs)
    try {
      return await this.locks.request(`${this.scope}:state`, { mode: "exclusive", signal: controller.signal }, async () => {
        clearTimeout(timer)
        this.waiters.delete(controller)
        if (this.closed) throw new BrowserGameError("disposed")
        return callback()
      })
    } catch (error) {
      if (controller.signal.aborted) throw new BrowserGameError(this.closed ? "disposed" : "busy")
      throw error
    } finally {
      clearTimeout(timer)
      this.waiters.delete(controller)
    }
  }

  // Shared barrier used to serialize revocation journal reads/writes with the
  // final consent grant and the validation-to-fetch-start boundary.
  async revocation(callback) {
    if (this.closed) throw new BrowserGameError("disposed")
    const controller = new AbortController()
    this.waiters.add(controller)
    const timer = setTimeout(() => controller.abort(), this.timeoutMs)
    try {
      return await this.locks.request(`${this.scope}:revocation`, { mode: "exclusive", signal: controller.signal }, async () => {
        clearTimeout(timer)
        this.waiters.delete(controller)
        if (this.closed) throw new BrowserGameError("disposed")
        return callback()
      })
    } catch (error) {
      if (controller.signal.aborted) throw new BrowserGameError(this.closed ? "disposed" : "busy")
      throw error
    } finally {
      clearTimeout(timer)
      this.waiters.delete(controller)
    }
  }

  async sender(callback) {
    if (this.closed) throw new BrowserGameError("disposed")
    return this.locks.request(`${this.scope}:sender`, { mode: "exclusive", ifAvailable: true }, lock => {
      if (!lock) throw new BrowserGameError("busy")
      if (this.closed) throw new BrowserGameError("disposed")
      return callback()
    })
  }

  dispose() {
    this.closed = true
    for (const controller of this.waiters) controller.abort()
    this.waiters.clear()
  }
}
