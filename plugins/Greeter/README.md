# Greeter

Sends a custom greeting message when a registered nick joins the channel.

## Commands

| Command | Description |
|---|---|
| `!addgreet <nick> <greeting>` | Add or replace a greeting for a nick |
| `!delgreet <nick>` | Remove a nick's greeting |
| `!listgreets` | List all stored greetings |

Greetings are stored in `Greeter.json` in the bot's data directory and persist across restarts.
