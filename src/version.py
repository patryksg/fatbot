version = '2026.05.20+fat'
try:
    import supybot.utils.python
    supybot.utils.python._debug_software_version = version
except ImportError:
    pass
