# Create

Image and video generation via Runware.ai (images) and Atlas Cloud (video). Prompts are automatically expanded with cinematic style modifiers via Claude Haiku before submission.

## Commands

| Command | Description |
|---|---|
| `!pic <prompt>` | Generate an image (Runware, SFW model) |
| `!picnsfw <prompt>` | Generate an image (Runware, NSFW model) |
| `!video <prompt>` | Generate image → animate via Atlas Wan 2.2 I2V |
| `!videonsfw <prompt>` | Generate NSFW image → animate via Atlas Spicy I2V |

NSFW commands are marked with a bold `[NSFW]` prefix in replies.

## Requirements

- `RUNWARE_API_KEY` environment variable
- `ATLAS_API_KEY` environment variable
- `claude` CLI for prompt expansion
- Channels must have the `generative` capability enabled
