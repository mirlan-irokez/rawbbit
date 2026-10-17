// Synthetic local-engine characterization, not a mobile/frame-cost guarantee.
import { chromium } from "@playwright/test"
import { readFile } from "node:fs/promises"
import { platform, release, arch } from "node:os"

const bundle = await readFile(new URL("../dist/index.js", import.meta.url), "utf8")
const browser = await chromium.launch()
try {
  const context = await browser.newContext()
  await context.route("**/*", route => {
    const url = route.request().url()
    if (url === "https://sdk-benchmark.invalid/sdk.js") return route.fulfill({ contentType: "text/javascript", body: bundle })
    if (url === "https://sdk-benchmark.invalid/") return route.fulfill({ contentType: "text/html", body: "<!doctype html><title>Synthetic benchmark</title>" })
    return route.abort()
  })
  const page = await context.newPage()
  await page.goto("https://sdk-benchmark.invalid/")
  const metrics = await page.evaluate(async () => {
    const { createBrowserGameClient, createLocalStorageRevocationStore } = await import("/sdk.js")
    const start = performance.now()
    const client = await createBrowserGameClient({ collectorUrl: "https://collector.invalid", apiKey: "synthetic-benchmark-key",
      appId: "synthetic-benchmark", environment: "test", autoFlush: false,
      revocationStore: createLocalStorageRevocationStore({ appId: "synthetic-benchmark", environment: "test" }) })
    const initializationMs = performance.now() - start
    await client.confirmConsent(() => true)
    let largestFrameGapMs = 0, previousFrame = performance.now(), stopped = false
    const frame = now => { largestFrameGapMs = Math.max(largestFrameGapMs, now - previousFrame); previousFrame = now; if (!stopped) requestAnimationFrame(frame) }
    requestAnimationFrame(frame)
    const seedStart = performance.now()
    for (let batch = 0; batch < 9; batch++) {
      const value = await client.trackBatch(Array.from({ length: 64 }, () => ({ eventName: "synthetic", eventParams: { snapshot: "x".repeat(17000) } })))
      if (value.status !== "durablyStored") throw new Error("benchmarkAdmissionFailed")
    }
    const seedMs = performance.now() - seedStart
    const diagnostics = await client.getDiagnostics()
    if (diagnostics.queueBytes < 0.9 * 10485760) throw new Error("benchmarkQueueNotNearCapacity")
    const admissionStart = performance.now()
    if ((await client.track({ eventName: "large_snapshot", eventParams: { snapshot: "x".repeat(900000) } })).status !== "durablyStored") throw new Error("benchmarkLargeSnapshotFailed")
    const largeSnapshotAndNearCapAdmissionMs = performance.now() - admissionStart
    const readStart = performance.now()
    await client.getDiagnostics()
    const nearCapDiagnosticsMs = performance.now() - readStart
    const purgeStart = performance.now()
    if (!(await client.withdrawConsent()).globalWithdrawalCommitted) throw new Error("benchmarkPurgeFailed")
    const nearCapPurgeMs = performance.now() - purgeStart
    await new Promise(resolve => requestAnimationFrame(resolve))
    stopped = true
    client.dispose()
    return { initializationMs, seedMs, seedRecords: 576, nearCapQueueBytes: diagnostics.queueBytes,
      largeSnapshotBytes: 900000, largeSnapshotAndNearCapAdmissionMs, nearCapDiagnosticsMs, nearCapPurgeMs, largestFrameGapMs }
  })
  console.log(JSON.stringify({ browser: `Chromium ${browser.version()}`, os: `${platform()} ${release()} ${arch()}`, metrics,
    limitations: "Single headless desktop run; no mobile hardware, power-failure, memory guarantee, or zero-gameplay-cost claim." }, null, 2))
} finally { await browser.close() }
