function fallbackRandomId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export function generateId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return fallbackRandomId()
}

export function nowIso() {
  return new Date().toISOString()
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {}
}

function firstDefined(...values) {
  for (const value of values) {
    if (value !== undefined) return value
  }
  return undefined
}

function firstNonNullish(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null) return value
  }
  return null
}

function normalizeUser(user = {}) {
  return {
    user_id: firstDefined(user.user_id, user.userId, null),
    user_pseudo_id: firstDefined(user.user_pseudo_id, user.userPseudoId, null),
    session_id: firstDefined(user.session_id, user.sessionId, null),
  }
}

function normalizeDevice(device = {}, defaultDevice = {}) {
  const merged = { ...asObject(defaultDevice), ...asObject(device) }
  return {
    platform: firstDefined(merged.platform, null),
    app_version: firstDefined(merged.app_version, merged.appVersion, null),
    os_version: firstDefined(merged.os_version, merged.osVersion, null),
    device_model: firstDefined(merged.device_model, merged.deviceModel, null),
    locale: firstDefined(merged.locale, null),
    timezone: firstDefined(merged.timezone, null),
  }
}

export async function buildCollectorEvent({ eventInput, config, identityAdapter }) {
  const input = asObject(eventInput)
  const eventName = firstDefined(input.eventName, input.event_name)
  if (!eventName) {
    throw new Error("eventName is required")
  }

  const eventTimestamp = firstNonNullish(input.eventTimestamp, input.event_timestamp, nowIso())
  const eventId = firstNonNullish(input.eventId, input.event_id, generateId())

  const adapter = identityAdapter || {}
  const pseudoIdFromAdapter =
    typeof adapter.getUserPseudoId === "function" ? await adapter.getUserPseudoId() : null
  const sessionIdFromAdapter =
    typeof adapter.getSessionId === "function" ? await adapter.getSessionId() : null

  const user = normalizeUser(input.user)
  user.user_pseudo_id = firstNonNullish(user.user_pseudo_id, pseudoIdFromAdapter)
  user.session_id = firstNonNullish(user.session_id, sessionIdFromAdapter)

  const defaultEventParams = {
    ...(config.product !== undefined ? { product: config.product } : {}),
    ...(config.surface !== undefined ? { surface: config.surface } : {}),
    ...(config.sourceType !== undefined ? { source_type: config.sourceType } : {}),
    ...(config.schemaVersion !== undefined ? { schema_version: config.schemaVersion } : {}),
  }

  const eventParams = {
    ...defaultEventParams,
    ...asObject(firstDefined(input.eventParams, input.event_params, {})),
  }

  return {
    event_id: String(eventId),
    app_id: config.appId,
    environment: config.environment,
    event_name: String(eventName),
    event_timestamp: String(eventTimestamp),
    user,
    device: normalizeDevice(input.device, config.defaultDevice),
    event_params: eventParams,
    user_properties: asObject(firstDefined(input.userProperties, input.user_properties, {})),
    traffic_source: asObject(firstDefined(input.trafficSource, input.traffic_source, {})),
    geo: asObject(firstDefined(input.geo, {})),
    consent: asObject(firstDefined(input.consent, {})),
  }
}
