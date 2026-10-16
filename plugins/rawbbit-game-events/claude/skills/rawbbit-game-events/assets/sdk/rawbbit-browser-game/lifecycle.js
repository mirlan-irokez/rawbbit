export function attachLifecycle(client, runtime) {
  const { window, document, BroadcastChannel } = runtime
  const removers = []
  const listen = (target, event, handler) => {
    target?.addEventListener?.(event, handler)
    removers.push(() => target?.removeEventListener?.(event, handler))
  }
  const restore = () => { client._suspended = false; client._invalidate("consentUnknown"); client._permissionRequired(); client._wake() }
  listen(window, "online", () => client._wake())
  listen(window, "pagehide", () => { client._suspended = true; client._invalidate("consentUnknown") })
  listen(window, "pageshow", event => { if (event.persisted) restore(); else client._wake() })
  listen(document, "freeze", () => { client._suspended = true; client._invalidate("consentUnknown") })
  listen(document, "resume", restore)
  listen(document, "visibilitychange", () => {
    if (document.visibilityState === "hidden") { client._hidden = true; client._abortRequest() }
    else { client._hidden = false; client._wake() }
  })
  let channel
  try {
    if (BroadcastChannel) {
      channel = new BroadcastChannel(client.scope)
      channel.onmessage = event => {
        if (event.data?.type === "revoke") {
          client._invalidate("consentUnknown")
          client._permissionRequired()
        }
        if (event.data?.type === "wake") client._wake()
      }
    }
  } catch { /* Advisory notifications are not authorization. */ }
  client._notify = type => { try { channel?.postMessage({ type }) } catch { /* Optional. */ } }
  const timer = setInterval(() => { client._maintain(); client._wake() }, 1000)
  timer.unref?.()
  return () => { clearInterval(timer); for (const remove of removers) remove(); channel?.close(); client._notify = () => {} }
}
