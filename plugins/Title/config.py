import supybot.conf as conf
import supybot.registry as registry


def configure(advanced):
    pass


Title = conf.registerPlugin('Title')

conf.registerChannelValue(Title, 'enable',
    registry.Boolean(False, """If enabled, fatbot will fetch the HTML <title>
    of any URL posted in this channel and post it back."""))

conf.registerChannelValue(Title, 'nonSnarfingRegexp',
    registry.Regexp(None, """URLs matching this regexp will not be snarfed in
    this channel. Empty disables the filter."""))

conf.registerChannelValue(Title, 'format',
    registry.String(':: %s', """Format string for the snarfed title. The
    literal %s is replaced by the page title."""))

conf.registerGlobalValue(Title, 'timeout',
    registry.Float(6.0, """HTTP request timeout in seconds (per attempt;
    redirects each get a fresh timeout)."""))

conf.registerGlobalValue(Title, 'maxBytes',
    registry.PositiveInteger(262144, """Maximum bytes to read from each URL
    while looking for the <title> tag."""))

conf.registerGlobalValue(Title, 'maxLength',
    registry.PositiveInteger(380, """Maximum byte length of the IRC reply line
    (excluding the IRC envelope). Lines longer than this are truncated with
    an ellipsis."""))

conf.registerGlobalValue(Title, 'cookiesFile',
    registry.String('', """Optional absolute path to a Netscape-format
    cookies.txt. When set, cookies from that file are merged into the shared
    HTTP session, scoped per domain by the cookie jar. Useful for sites that
    block unauthenticated requests (e.g. reddit.com). The file is reloaded
    automatically when its mtime changes."""))

conf.registerGlobalValue(Title, 'userAgent',
    registry.String('Mozilla/5.0 (compatible; fatbot-title/1)',
    """User-Agent header sent when fetching URLs (ignored when curl_cffi is
    installed -- impersonation dictates the headers)."""))

conf.registerChannelValue(Title, 'useShrinkUrl',
    registry.Boolean(False, """If True, ask the ShrinkUrl plugin to shorten
    the URL using its configured service and post output as
    '<short> | <title>'. You should also disable
    `supybot.plugins.ShrinkUrl.shrinkSnarfer` for this channel to avoid
    duplicate output."""))
