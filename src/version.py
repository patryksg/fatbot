version = '2026.05.25+fat'
try:
    import supybot.utils.python
    supybot.utils.python._debug_software_version = version
except ImportError:
    pass
