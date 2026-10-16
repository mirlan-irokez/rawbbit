# Engine and SDK selection

Use `../assets/sdk/BUNDLE-MANIFEST.json` for bundled versions and file hashes; it does not identify a remote source checkout.
Read the relevant bundled SDK `README.md` and `INTEGRATION.md` before implementation; these documents, not this summary, define API names, queue/recovery semantics, and installation details.
The plugin does not need access to another repository or a published Rawbbit npm package.

## Support finding

Report three distinct claims:
(1) an event-map proposal can be made from game evidence;
(2) a bundled SDK is an integration candidate;
(3) that exact editor/browser/backend/device target passed its required gates.
Never infer (3) from (1) or (2).

- Unity: inspect `ProjectSettings/ProjectVersion.txt`, scripting backend, target platforms, Newtonsoft package compatibility, persistent-data behavior, bootstrap lifetime, and consent owner. The source uses Unity 2022.3 LTS as a **provisional API baseline**, not a certification. The reference has not completed Unity Editor compilation or Android/iOS player/device validation. Build and run target Editor/Android/iOS gates before a verified claim. Unity WebGL requires a separate capability assessment; neither SDK is automatically a WebGL solution.
- Browser JavaScript: check secure **top-level** context, IndexedDB, Web Locks, Fetch, AbortController, `structuredClone`, TextEncoder, secure random generation, a separate revocation journal, and the game's CSP/CORS/storage environment. Workers and third-party iframes/embedded portal contexts are outside this SDK's documented contract. Playwright WebKit does not certify Safari/iOS devices.
- Another engine: propose events if game evidence exists, but write a capability gap for consent/withdrawal, identity/session, durable queue and retry, stable IDs, transport, config/key handling, and lifecycle before an adapter port. Do not copy Unity C# or browser JS into a mismatched runtime. A port needs its own approved scope and target-platform verification.

## Four installed-SDK cases

1. **No SDK:** install the complete relevant snapshot from `../assets/sdk/`; keep its `LICENSE` and record its public manifest version and hashes. Follow its bundled integration guide and adapt, rather than treating, its example as production-ready game logic.
2. **Compatible SDK already installed:** reuse it. Map its version/copy revision and adapter ownership; add approved game events without copying a new SDK, duplicating a host, or changing its queue/identity.
3. **Upgrade requested or required:** compare current and target APIs, adapter template, Collector envelope, consent/withdrawal, persisted queue/storage, identity/session, endpoint binding, and effective rollback. Require a game-specific migration and platform tests. Unity's newer stable store layout cannot be safely downgraded to a URL-keyed build by copying old files back. Browser template copies do not upgrade with SDK source.
4. **Locally modified copy:** diff against known provenance and identify the fork owner. Do not overwrite or silently replace it. Propose a merge/compatibility path for approval.

An older direct-send `rawbbit-client` integration is not equivalent to the durable browser-game SDK. Treat moving between them as a migration: identity and storage are not automatically imported, and consent/adapter behavior differs.

## Configuration

Both SDKs take an HTTPS Collector URL, `app_id`, environment, and app-scoped ingest key from game-owned runtime configuration. The Collector maps each key to one `app_id`.
The target game's established config convention determines exact files and variable names; inspect it, then tell the human where to enter values. Put only placeholders in source.
Browser/mobile keys are extractable from client builds, not backend secrets.
For direct browser delivery confirm exact-origin CORS, CSP `connect-src`, key rotation, and abuse-control ownership; use a game-owned proxy only when chosen deliberately.
Never send live events merely to prove wiring.

Consent starts unknown on each SDK initialization. Do not infer a grant from game launch or an absent consent screen.
Read each SDK's host-gate/withdrawal instructions; in particular, the browser copy-in adapter has safety-critical host callbacks, and direct client use does not supply that facade automatically.
