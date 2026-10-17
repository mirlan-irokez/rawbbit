function safeCounter(value) {
  if (!Number.isSafeInteger(value) || value < 0) throw new RangeError("Session counter must be a nonnegative safe integer")
}

export function emptySession() {
  return { id: null, lastActivity: 0, counter: 0 }
}

// Pure candidate preparation: only an atomic successful admission commits this state.
export function sessionCandidate(previous, now, timeoutMs, createId) {
  if (!previous || typeof previous !== "object" ||
      (previous.id !== null && (typeof previous.id !== "string" || previous.id.length === 0))) {
    throw new TypeError("Invalid previous session")
  }
  safeCounter(previous.counter)
  if (!Number.isFinite(previous.lastActivity) || !Number.isFinite(now) ||
      !Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new TypeError("Invalid session activity or timeout")
  const isNewSession = previous.id === null || now - previous.lastActivity > timeoutMs
  const id = isNewSession ? createId() : previous.id
  if (typeof id !== "string" || id.length === 0) throw new TypeError("Session ID must be a nonempty string")
  // A candidate at exhaustion cannot admit even one input event. Never silently wrap.
  if (!isNewSession && previous.counter === Number.MAX_SAFE_INTEGER) throw new RangeError("Session counter exhausted")
  return Object.freeze({ id, isNewSession, counter: isNewSession ? 0 : previous.counter,
    lastActivity: isNewSession ? now : Math.max(now, previous.lastActivity) })
}

export function prepareSession(previous, now, timeoutMs, createId) {
  return sessionCandidate(previous, now, timeoutMs, createId)
}

export function eventContext(candidate, sequence, eventTimestamp, hasPrefix = false) {
  if (!candidate || typeof candidate.id !== "string" || candidate.id.length === 0 ||
      typeof candidate.isNewSession !== "boolean") throw new TypeError("Invalid session candidate")
  safeCounter(sequence)
  if (sequence === 0) throw new RangeError("Event sequence starts at one")
  if (typeof eventTimestamp !== "string" || eventTimestamp.length === 0) throw new TypeError("Event timestamp is required")
  if (typeof hasPrefix !== "boolean") throw new TypeError("hasPrefix must be boolean")
  return Object.freeze({ sessionId: candidate.id, isNewSession: candidate.isNewSession,
    eventSequence: sequence, sessionStartedEventSequence: hasPrefix ? 1 : 0,
    triggeringEventSequence: hasPrefix && sequence === 1 ? 2 : sequence, eventTimestamp })
}
