# Agent plugins: Rawbbit Game Event Tracking

Rawbbit ships a coding-agent plugin that helps you design, integrate, migrate, and maintain game-specific event tracking in a Unity or browser-JavaScript game. It inspects your game, proposes a reviewable event map, and, after you approve specific events, integrates them with the bundled Rawbbit SDK and verifies the result against a mocked Collector.

The same workflow comes in two packages:

| Package | Folder | Works with | Remote service |
|---|---|---|---|
| Claude | [`plugins/rawbbit-game-events/claude/`](../plugins/rawbbit-game-events/claude/README.md) | Claude chat, Cowork, and Claude Code | None (skills only) |
| Codex | [`plugins/rawbbit-game-events/codex/`](../plugins/rawbbit-game-events/codex/README.md) | ChatGPT desktop (Codex) | Optional public, read-only SDK catalog at `https://sdk-catalog.rawbbit.one/mcp` |

Both packages include complete Unity and browser-game SDK snapshots, so they work without access to this repository. Neither sends your game source, event payloads, `app_id`, or ingest keys anywhere. Each plugin README has a **What it runs and sends** and a **Privacy** section.

## Download

Each version is a GitHub release of this repository with the plugin ZIP and a checksum:

- Claude: releases tagged `claude-plugin-v<version>`, asset `rawbbit-game-events-claude-<version>.zip`
- Codex: releases tagged `codex-plugin-v<version>`, asset `rawbbit-game-events-<version>.zip`

See the [releases page](https://github.com/mirlan-irokez/rawbbit/releases). Verify a download next to its checksum file:

```bash
shasum -a 256 -c rawbbit-game-events-claude-<version>.zip.sha256
```

Each ZIP contains one top-level folder, `rawbbit-game-events/`.

## Quick install from this repository

This repository is also a plugin marketplace for both apps. It installs the plugin version currently on the `main` branch.

Claude Code:

```bash
claude plugin marketplace add mirlan-irokez/rawbbit
claude plugin install rawbbit-game-events@rawbbit
```

Inside a Claude Code session, the same steps are `/plugin marketplace add mirlan-irokez/rawbbit` and `/plugin install rawbbit-game-events@rawbbit`. In Claude chat and Cowork, add the GitHub repository `mirlan-irokez/rawbbit` as a marketplace under **Customize > Plugins**, then install **rawbbit-game-events** from it.

Codex:

```bash
codex plugin marketplace add mirlan-irokez/rawbbit
codex plugin add rawbbit-game-events@rawbbit
```

Restart the ChatGPT desktop app afterwards.

To update later:

- Claude Code: `claude plugin marketplace update rawbbit`, then `claude plugin update rawbbit-game-events@rawbbit`
- Codex: `codex plugin marketplace upgrade rawbbit`, then `codex plugin add rawbbit-game-events@rawbbit` and restart the ChatGPT desktop app

The sections below install from a downloaded release ZIP instead.

## Claude

### Claude chat and Cowork (web and desktop app)

1. Download the Claude plugin ZIP from the latest `claude-plugin-v*` release.
2. Open **Customize > Plugins**, choose **Add > Upload plugin**, and select the ZIP.
3. In Cowork or Claude Code, open your game folder and ask, for example, "Propose a Rawbbit event map for this game without changing code."

The plugin is installed on your claude.ai account and also appears in Claude Code as a synced plugin at the next session start. In chat without project files, the plugin can only produce an event proposal or capability gap report; it says that no files were inspected or changed.

### Claude Code (terminal, IDE, and desktop Code tab)

To try the plugin for one session from a downloaded copy:

```bash
unzip rawbbit-game-events-claude-<version>.zip
claude --plugin-dir ./rawbbit-game-events
```

Run the second command from your game's folder, or start Claude Code there and pass the full path to the unzipped plugin.

### From the Claude plugin directory

The Claude plugin is submitted to Anthropic's plugin directory. Once it is listed, add it from **Customize > Plugins > Discover** in Claude, or with `/plugin directory` in Claude Code.

## Codex (ChatGPT desktop app)

The Codex plugin requires a local game checkout and desktop execution. Until it is listed in OpenAI's plugin directory, install it in one of these ways.

### Upload the ZIP

1. Download the Codex plugin ZIP from the latest `codex-plugin-v*` release.
2. In the ChatGPT/Codex desktop app, open **Plugins**, click **Add** -> **Upload plugin archive** and upload the ZIP.
3. Open your game folder and ask, for example, "Inspect this game and propose a Rawbbit event map without changing code."

### From a personal local marketplace

1. Download the Codex plugin ZIP from the latest `codex-plugin-v*` release and unpack it into `~/.codex/plugins/`:

   ```bash
   mkdir -p ~/.codex/plugins
   unzip rawbbit-game-events-<version>.zip -d ~/.codex/plugins
   ```

   The plugin is now in `~/.codex/plugins/rawbbit-game-events/`.

2. Create or edit `~/.agents/plugins/marketplace.json`. The `path` is relative to your home directory (the marketplace root) and must start with `./`:

   ```json
   {
     "name": "rawbbit-local",
     "plugins": [
       {
         "name": "rawbbit-game-events",
         "source": { "source": "local", "path": "./.codex/plugins/rawbbit-game-events" },
         "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
         "category": "Developer Tools"
       }
     ]
   }
   ```

   If the file already lists other plugins, add the entry to its `plugins` array.

3. Restart the ChatGPT desktop app, open the **Plugins** directory, choose the `rawbbit-local` marketplace, and install **Rawbbit Game Event Tracking**.

To update, replace the folder with the new version's contents and restart the app again. The optional SDK catalog connects without sign-in; if it is unreachable, the plugin keeps using its bundled SDKs.

With the Codex CLI, you can instead add the unzipped plugin's parent folder as a marketplace only if it contains `.agents/plugins/marketplace.json`; the quick install above uses this repository's marketplace file for that.

## Limits

The plugin does not certify Unity Editor compilation, Android/iOS devices, Unity WebGL, or embedded and third-party-iframe browser contexts; test those in your game. Other engines receive a capability gap report instead of a mismatched SDK. The plugin tests against a mocked Collector and sends no live events unless you explicitly authorize it.

## Support

Report bugs and request features in this repository's [GitHub Issues](https://github.com/mirlan-irokez/rawbbit/issues), or email team@rawbbit.one.
