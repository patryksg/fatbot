from importlib import reload

from . import config
from . import plugin

reload(plugin)

Class = plugin.Class
configure = config.configure
