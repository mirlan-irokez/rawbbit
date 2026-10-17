# Rawbbit Game Event Tracking for Codex

This Codex plugin helps you design, integrate, migrate, and maintain game-specific event tracking for [Rawbbit](https://rawbbit.one/), a self-hosted game analytics pipeline. It works on Unity and browser-JavaScript games and includes complete Unity and browser-game SDK source snapshots, integration guides, adapter templates, licenses, and Collector contract facts. An optional public, read-only catalog service reports which SDK revisions are current and what is known about your target platform.

## What it does

- Inspects your game's code and confirms its engine and target platform with you.
- Inventories existing Rawbbit and other-analytics events before proposing changes.
- Writes a reviewable event map (by default `docs/analytics/rawbbit-event-map.md`) with triggers, fields, consent, and identity decisions.
- After you approve specific events, integrates them through a game-owned adapter using the bundled SDK, including dual-write alongside another analytics tool.
- Optionally checks the public SDK catalog for a newer SDK revision or target-compatibility evidence. A newer revision is only ever proposed as a separate upgrade for you to approve.
- Verifies the integration against a mocked Collector and reports which target-platform checks were not run.

## Requirements

The plugin works on a local game checkout and requires desktop execution. Unity Editor compilation, Android/iOS devices, Unity WebGL, and embedded or third-party-iframe browser contexts must be tested in your game; the plugin does not certify them. Other engines receive a capability gap report rather than a mismatched SDK.

## What it runs and sends

- **In your game project**, the skill may copy one bundled SDK into the game, edit game code you approved, and run the game's own build and test commands. For the browser SDK that includes `npm ci`, `npm test`, and `npm run build` inside the copied SDK folder, which download public npm packages listed in its lockfile. For Unity it uses the game's Unity Editor and build tooling.
- **To the public SDK catalog** at `https://sdk-catalog.rawbbit.one/mcp` (no sign-in), the skill may send only generic target details: the engine, platform, engine version, scripting backend, whether the page is a secure top-level browser context, which browser capabilities are available, and optionally the installed SDK version or catalog revision. The catalog returns public SDK revisions, file hashes, compatibility evidence, and Collector schema facts. It never receives game source, event names or payloads, `app_id`, ingest keys, account identifiers, or consent records.
- **If the catalog is unavailable**, the skill keeps using the bundled SDKs and reports that live compatibility was not verified.
- **The integrated SDK** sends gameplay events only to the Rawbbit Collector that you configure in your game, and only after the game's consent gate allows it. The skill tests against a mocked Collector and does not send live traffic unless you explicitly authorize it.
- **Credentials:** the skill asks where your game stores its app-scoped ingest key, never for the key value, and commits only placeholders.

## Privacy

- **No personal data collection.** The plugin collects and stores no personal or usage data, and Rawbbit receives nothing about you or your game from installing or using it.
- **Catalog requests.** Lookups to the SDK catalog contain only the generic target details listed above. The service keeps caller network addresses briefly in memory to enforce rate limits; its logs record only a request ID, method, path, status, and duration, and it stores no request bodies. Its web server keeps no access logs, though its error logs may include a failed request's network address.
- **Local project only.** The skill reads and edits files only in the game checkout you open. Event maps, code changes, and test output stay in that project.
- **Your game's telemetry.** If you integrate the bundled SDK, your game sends gameplay events to the Rawbbit Collector that you configure and operate, only after your game's consent gate allows it. Rawbbit does not host or receive that data. Your own privacy policy and player-consent flow govern it.

## Support

Report bugs and request features in the Rawbbit repository's GitHub Issues, or email team@rawbbit.one.

## Contents

- [Skill workflow](skills/rawbbit-game-events/SKILL.md)
- [Unity SDK](skills/rawbbit-game-events/assets/sdk/rawbbit-unity/README.md) and [browser-game SDK](skills/rawbbit-game-events/assets/sdk/rawbbit-browser-game/README.md): copy the appropriate complete SDK into the game; do not build or install dependencies inside the plugin copy, which would invalidate its file inventory.
- [Bundle manifest](skills/rawbbit-game-events/assets/sdk/BUNDLE-MANIFEST.json): SDK versions and file hashes. Run `python3 scripts/verify_bundle.py` from the plugin folder to check them; hashes detect changes but do not authenticate a publisher.
- [Collector contract facts](skills/rawbbit-game-events/references/collector-contract.json) and [field ownership guidance](skills/rawbbit-game-events/references/schema-and-field-ownership.md).
- [License](LICENSE): Apache-2.0. Each copyable SDK snapshot includes its own license text.
