import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    pass


Create = conf.registerPlugin('Create')

conf.registerChannelValue(Create, 'model',
    registry.String('civitai:1195276@1345786',
        'Runware AIR model id (default: Lustify SDXL no-login mirror).'))

conf.registerGlobalValue(Create, 'timeoutSec',
    registry.PositiveInteger(120,
        'HTTP timeout for the Runware image generation request.'))

conf.registerGlobalValue(Create, 'videoTimeoutSec',
    registry.PositiveInteger(900,
        'Total time to wait for video generation (Atlas/fal) to finish.'))

conf.registerChannelValue(Create, 'picModel',
    registry.String('civitai:133005@1759168',
        'Runware AIR for !pic and seed image of !video (default: Juggernaut XL v11, SFW-tauglich).'))
