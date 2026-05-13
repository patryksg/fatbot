# ChanModes

Auto-enforces configured channel modes when the bot has ops.

When the bot joins a channel or a mode change occurs, it checks the desired modes against the current channel state and reissues any missing ones.

## Configuration

```
!config channel #yourchannel plugins.ChanModes.modes +pnst
```

Set to an empty string to disable enforcement on a channel:

```
!config channel #yourchannel plugins.ChanModes.modes ""
```
