# Reload

One-shot batch reloader for the bot's local plugins.

Stock Limnoria's `reload` takes one plugin at a time and Alias can only map a
single command, so there's no built-in way to reload everything after a round
of plugin edits. This plugin adds `!rl`.

## Commands

| Command | Description |
|---|---|
| `!rl` | Reload every plugin listed in `supybot.plugins.Reload.plugins`, in order. Owner-only. |

Reports inline: `reloaded 12: Ash, Claude, ...` — failures are listed
separately with the error (`FAILED 1: Foo (no such module)`).

## Configuration

```
config supybot.plugins.Reload.plugins Ash Claude Create EasyControl ...
```

The plugin deliberately skips itself — you can't hot-swap the plugin whose
code is mid-execution (same self-guard as Owner.reload).

## Implementation note

`_reload_one()` replicates `Owner.reload` exactly: `removeCallback` →
`loadPluginModule` → `loadPluginClass`, with `module.reload()` state transfer
and a `gc.collect()` in between. It is a standalone plugin (not part of
EasyControl) so it can safely reload EasyControl too.

## First-time activation

```
!load Reload
```

(`!reload Reload` won't work the first time — it isn't loaded yet.)
