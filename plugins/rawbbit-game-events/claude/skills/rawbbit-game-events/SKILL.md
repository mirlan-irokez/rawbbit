---
name: rawbbit-game-events
description: Design, document, implement, migrate, or maintain Rawbbit event tracking in a Unity or browser-JavaScript game, including existing analytics and dual-write decisions. Use when the user wants to add game analytics or telemetry, instrument or track gameplay events, design an event map or tracking plan, integrate the Rawbbit Unity or browser-game SDK, or migrate events from another analytics tool such as Firebase or GameAnalytics.
---

# Rawbbit game events

## Claude execution surface

In Claude Code or Cowork, inspect and change a game only when its local checkout is available. In Claude chat or any context without project files, provide an event-map proposal or capability gap report only; say explicitly that no files were inspected or changed and do not claim integration or target verification.

Make the game's event map the reviewable contract.
The plugin contains a pinned SDK source inventory, not SDK code. A new SDK copy requires the approved, verified download below.
Do not treat a sample game or SDK template as the target game's event catalog.
Do not send to a deployed Collector unless the human explicitly authorizes live traffic.

## Select the path

1. Inspect the game code, project manifests, target runtime, build settings, gameplay transitions, existing event documents, analytics adapters, SDK copies, consent/identity owner, and runtime configuration. Record observed mechanics separately from design-document plans. Show the evidence for engine and target, and confirm it with the human unless that exact choice is already authorized.
2. Read [engine and SDK selection](references/engine-and-sdk.md). Classify the exact target as event-map proposal possible, SDK integration candidate, or verified by target tests. Unity WebGL, third-party iframe/portal browser games, and other engines are not silently treated as covered. For an unsupported target, produce a capability gap and a separately scoped adaptation design; do not install a mismatched SDK or claim production parity.
3. Inventory current Rawbbit and other-analytics events before proposing changes. For each, capture name, meaning, committed trigger/call sites, parameters/types/units, identity, consent, frequency, consumers, and delivery owner. Bound dynamic names. Choose explicitly among new install, reuse compatible SDK, deliberate upgrade, or locally modified SDK; adding an event does not authorize an SDK upgrade. Read [migration and maintenance](references/migration.md) when existing instrumentation or another destination is present.

## Produce one review package

4. Ask for missing **non-secret** Collector endpoint, `app_id`, environment, target platforms, delivery topology, and the game's existing config file/variable/deployment setting for the app-scoped ingest key. Tell the human where `app_id` and key belong; ask for the key's location, never its value in chat. Use placeholders in committed examples. Browser/mobile client keys are extractable, scoped credentials. Identify the consent policy source and approved `user_id` source; use SDK pseudonymous identity by default when there is no account decision.
5. Read [event design](references/event-design.md), [Collector schema versus field ownership](references/schema-and-field-ownership.md), and the [event-map template](references/event-map-template.md). Create or update the game's existing analytics document, otherwise `docs/analytics/rawbbit-event-map.md`. Include all discovered current Rawbbit events. Recommend only candidates justified by observed mechanics and an analytical decision; give each exact trigger, deduplication rule, field source, type/unit, volume, destination, consent, identity, status, and compatibility classification. The [worked example](references/worked-example.md) illustrates evidence separation; it is not a catalog to copy.
6. Present engine/SDK evidence, configuration gaps, event-map diff, migration options, and verification limits together. Identify new events, optional additions, compatibility-sensitive changes, and deprecations separately. Respect approval already given for exact changes; otherwise obtain approval of the concrete package before instrumentation. Individually name every contract change/deprecation, but the human may approve them in one response. Implement only the unambiguous approved subset; unapproved candidates remain marked proposed.

## Implement and verify

7. Integrate through a game-owned adapter using the compatible installed SDK or the approved, verified SDK download below. The SDK/runtime stays generic; game code owns committed business transitions. For dual-write, fan out application-owned inputs to independent adapters. Neither adapter may require the other's success or import its generated identity, session, consent cache, autocapture payload, or storage. Preserve existing emissions unless replacement is approved. Do not block gameplay on telemetry delivery. Record SDK and copied adapter provenance in the event map.
8. Follow [verification](references/verification.md). Mock the Collector by default. Assert the approved event set per destination and documented session prefix, rerun idempotency, field ownership separately from Collector schema acceptance, consent withdrawal/restart, identity, offline/fault behavior, adapter failure isolation, and target-project checks. Distinguish extra gameplay triggers from at-least-once delivery retries retaining event IDs. Report skipped Editor/device/browser gates rather than treating a mock as platform certification.
9. Handoff the event-map/code diff, approved/deferred items, SDK path/provenance, verification evidence, config locations without key values, deployment gaps, and any remaining migration risk.

## Approved SDK download and verification

Only after the human approves the integration and selected SDK, explain that you will download the complete selected SDK directory and root `LICENSE` from https://github.com/mirlan-irokez/rawbbit at the full commit in `references/sdk-source.json`. Use only that repository and commit—never a branch, tag, latest, registry, or another repository.

Create a fresh temporary directory outside the game. If Git is installed, run:

```sh
git init <tmp>
git -C <tmp> -c core.autocrlf=false -c credential.helper= fetch --depth 1 https://github.com/mirlan-irokez/rawbbit.git <full-sha>
git -C <tmp> -c core.autocrlf=false checkout FETCH_HEAD -- sdk/<name> LICENSE
```

Use archive fallback only when Git is unavailable: download https://github.com/mirlan-irokez/rawbbit/archive/<full-sha>.tar.gz and safely extract only `sdk/<name>/` and root `LICENSE` to the temporary directory. Reject absolute or traversal paths, symlinks, hard links, and nonregular files. If Git fetch, network, download, extraction, or any later check fails, stop—do not fall back from a Git failure, do not copy anything into the game, report the failure, and clean up the temporary directory. Never extract into the game.

Read `references/sdk-source.json`. Compare the exact case-sensitive relative paths under `sdk/<name>/` with that SDK's complete `files` keys: every listed regular file must exist, and no unlisted file may exist. Hash raw bytes with `shasum -a 256`, `sha256sum`, or PowerShell `Get-FileHash -Algorithm SHA256`; compare every SHA-256 and the root `LICENSE` hash case-insensitively as hex. Do not normalize line endings. Any path, missing-file, unexpected-file, or hash error stops the process before copying.

After all checks succeed, read the selected verified `README.md` and `INTEGRATION.md`. Follow the verified `INTEGRATION.md` for what to copy and where (for Unity it copies `Runtime/` to `Assets/Rawbbit/`; for browser it copies the complete SDK directory including `vendor/`). Preserve the verified Apache-2.0 `LICENSE` and attribution. Do not overwrite an installed or modified SDK without the separately approved migration path. Record the repository, commit, selected SDK version, and hashes in the event map. Clean up the temporary directory after copying.

`references/sdk-source.json` also records the hash of `references/collector-contract.json`. The inventory detects mismatches but does not authenticate the publisher.
