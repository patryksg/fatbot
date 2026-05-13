# InfoToggle

Admin shortcuts for common bot management tasks. Combines user management, capability gating, and per-channel plugin toggling into convenient commands.

## Commands

| Command | Description |
|---|---|
| `!info [<channel>]` | Show which plugins are active in a channel |
| `!ai <on\|off> [<channel>]` | Enable/disable the Claude plugin in a channel |
| `!chanmode <on\|off> [<channel>]` | Enable/disable ChanModes enforcement in a channel |
| `!adduser <nick> [<password>]` | Register a nick with the bot (generates password if omitted) |
| `!deluser <nick>` | Remove a registered user |
| `!cap <nick> <capability>` | Grant a capability to a user |
| `!remcap <nick> <capability>` | Remove a capability from a user |

All commands require the `admin` capability.
