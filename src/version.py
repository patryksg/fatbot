version = '2026.05.12+fat'
try:
    import supybot.utils.python
    supybot.utils.python._debug_software_version = version
except ImportError:
    pass
