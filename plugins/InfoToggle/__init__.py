from . import plugin
from importlib import reload
reload(plugin)
Class = plugin.Class
