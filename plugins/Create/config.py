import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    pass


Create = conf.registerPlugin('Create')

conf.registerChannelValue(Create, 'model',
    registry.String('bfl:3@1',
        'Runware model id for !picnsfw (default: Flux Pro, NSFW-capable).'))

conf.registerGlobalValue(Create, 'timeoutSec',
    registry.PositiveInteger(120,
        'HTTP timeout for the Runware image generation request.'))

conf.registerGlobalValue(Create, 'videoTimeoutSec',
    registry.PositiveInteger(900,
        'Total time to wait for video generation (Atlas/fal) to finish.'))

conf.registerChannelValue(Create, 'picModel',
    registry.String('bfl:3@1',
        'Runware model id for !pic and seed image of !video (default: Flux Pro, high quality).'))

conf.registerChannelValue(Create, 'editModel',
    registry.String('bfl:3@1',
        'Runware model id for instruction edits of !picnsfw <url> <edit> '
        '(default: FLUX.1 Kontext Pro). SFW-only; NSFW edits fall back to editFallbackModel.'))

conf.registerChannelValue(Create, 'editFallbackModel',
    registry.String('civitai:1195276@1345786',
        'Uncensored Runware model (Lustify SDXL) for the img2img fallback when '
        'Kontext refuses an NSFW edit on !picnsfw <url> <edit>.'))

conf.registerChannelValue(Create, 'editStrength',
    registry.String('0.6',
        'img2img strength (0-1) for the uncensored NSFW edit fallback; higher = '
        'further from the source image.'))
