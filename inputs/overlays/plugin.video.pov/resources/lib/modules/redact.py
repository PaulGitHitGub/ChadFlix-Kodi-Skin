"""ChadFlix R10.25 (security audit F-02): keep sign-in secrets out of Kodi's log and out of an uploaded log.

    clean(text, secrets=())      one message or a whole log, with credentials replaced by [REDACTED]
    safe_url(url)                "host/path" of a URL for a log line: no scheme user, no query, no token-like path part
    known_secrets(table)         the values of POV's own sign-in settings, to pass to clean() (never logged or stored)

What clean() replaces:
  * user:password@ in any URL;
  * the value of a credential-named query parameter (token, apikey, key, code, pin, password, ...), plain or URL-encoded,
    and every query value of an http(s) URL when strict=True (the upload);
  * Authorization / Cookie / X-Api-Key header values, and "Bearer xxx" / "Basic xxx" anywhere;
  * the value of a credential-named field in JSON / Python dict text, in XML elements and attributes, and in
    <setting id="...token">value</setting> lines;
  * dotted tokens (JWT and similar three-part tokens);
  * token-like path parts of a URL (long opaque parts, long hex), and everything after the first path part of a debrid
    download link;
  * every exact value given in `secrets`, also in its URL-encoded forms.

This is pattern matching: it removes the credential shapes above and POV's own stored secrets. It cannot promise to find
every secret another add-on may have written to the log in a shape of its own. No Kodi import: testable anywhere.
"""
import re
from urllib.parse import quote, quote_plus

MASK = '[REDACTED]'

# Names that always mean a credential, as a whole name or inside one (rd.token, access_token, client_secret, api-key ...).
_STRONG = (r'[A-Za-z0-9_.\-]*(?:token|secret|passw(?:or)?d|passwd|api[_\-]?key|apikey|authorization|session[_\-]?id'
           r'|device[_\-]?code|user[_\-]?code|client[_\-]?id|refresh|credential)[A-Za-z0-9_.\-]*')
# Short names that are credentials only as a URL query parameter or an exact field name.
_WEAK = r'(?:key|code|pin|pass|pwd|auth|sig|signature|sid|user_ip|ip)'
_VALUE = r'''[^&\s"'<>#;,)\]}]'''

# Hosts whose links are the playable file itself: everything after the first path part is masked.
_LINK_HOSTS = re.compile(r'(?:^|\.)(?:real-debrid\.com|rdeb\.io|torbox\.app|tb-cdn\.st|premiumize\.me|energycdn\.com|'
                         r'alldebrid\.com|debrid\.it|offcloud\.com|easynews\.com|pikpak\.io|mypikpak\.com)$', re.I)

_M = '\x00'     # stand-in for MASK while cleaning: one character no rule can split or match twice
_RULES = None


def _rules():
    global _RULES
    if _RULES is None:
        flags = re.I
        _RULES = {
            'userinfo': re.compile(r'\b([a-z][a-z0-9+.\-]{1,15}://)[^/\s:@"\'<>]+(?::[^/\s@"\'<>]*)?@', flags),
            'header': re.compile(r'''\b(authorization|proxy-authorization|x-api-key|cookie|set-cookie)(["']?\s*[:=]\s*["']?)'''
                                 r'''(?:(bearer|basic|token|digest)\s+)?[^"'\r\n,}]+''', flags),
            'bearer': re.compile(r'\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}', flags),
            'strong_kv': re.compile(r'(?<![A-Za-z0-9_.\-])(' + _STRONG + r')=(' + _VALUE + r'+)', flags),
            'weak_query': re.compile(r'([?&]' + _WEAK + r')=(' + _VALUE + r'+)', flags),
            'encoded_kv': re.compile(r'((?:%3F|%26)(?:' + _STRONG + '|' + _WEAK + r'))%3D((?:(?!%26)' + _VALUE + r')+)', flags),
            'field': re.compile(r'''(["'])(''' + _STRONG + '|' + _WEAK + r''')\1(\s*:\s*)(["'])(.*?)\4''', flags),
            'field_bare': re.compile(r'''(["'])(''' + _STRONG + r''')\1(\s*:\s*)([^\s"',}\]]+)''', flags),
            'xml_setting': re.compile(r'(<setting\b[^>]*\bid="(?:' + _STRONG + r')"[^>]*>)([^<]+)(</setting>)', flags),
            'xml_element': re.compile(r'(<(' + _STRONG + r')(?:\s[^>]*)?>)([^<]+)(</\2>)', flags),
            'xml_attr': re.compile(r'(\s(?:' + _STRONG + r')=)(["\'])(.*?)\2', flags),
            'jwt': re.compile(r'\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}(?:\.[A-Za-z0-9_\-]{6,})?'),
            'dotted': re.compile(r'(?<![A-Za-z0-9_\-.])[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}(?![A-Za-z0-9_\-.])'),
            'url': re.compile(r'\bhttps?://[^\s"\'<>\[\]{}|\\^`]+', flags),
            'opaque': re.compile(r'^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9_\-=~+%]{20,}$'),
            'hex': re.compile(r'^[0-9a-fA-F]{32,}$'),
        }
    return _RULES


_API_LABELS = ('api', 'app', 'www', 'auth')   # a service's API / site host, not a file host: its paths stay readable
_PLAIN_NAMES = ('token_type', 'tokentype')    # credential-looking names whose value is not one


def _plain(match, name):
    return match.group(0) if name.lower() in _PLAIN_NAMES else None


_SLUG = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)+$')   # the-dark-knight-2008: a readable name, kept in ordinary log lines


def _clean_path(host, path, rules, strict=True):
    parts = path.split('/')
    if _LINK_HOSTS.search(host or '') and (host or '').split('.')[0].lower() not in _API_LABELS:
        # ['', 'd', 'ABC', 'file.mkv'] -> /d/[REDACTED]
        return '/'.join(parts[:2] + [_M]) if len(parts) > 2 and any(parts[2:]) else path
    return '/'.join(_M if (rules['opaque'].match(part) and (strict or not _SLUG.match(part))) or rules['hex'].match(part) else part
                    for part in parts)


def _clean_url(match, rules, strict):
    url = match.group(0)
    tail = ''
    while url and url[-1] in '.,;:!?)':       # sentence punctuation after a URL is not part of it
        url, tail = url[:-1], url[-1] + tail
    scheme, _, rest = url.partition('://')
    netloc, slash, after = rest.partition('/')
    for mark in ('?', '#'):                    # a query right after the host, with no path
        if mark in netloc:
            netloc, _, extra = netloc.partition(mark)
            after, slash = mark + extra, ''
            break
    path, question, query = (slash + after).partition('?')
    host = netloc.rsplit('@', 1)[-1].split(':')[0]
    path = _clean_path(host, path, rules, strict)
    if question and strict:
        query, hash_mark, fragment = query.partition('#')
        query = '&'.join((pair.partition('=')[0] + '=' + _M) if '=' in pair else pair for pair in query.split('&'))
        query += hash_mark + fragment
    return scheme + '://' + netloc + path + question + query + tail


def clean(text, secrets=(), strict=True):
    """`text` with credentials masked. strict=True also masks every query value of an http(s) URL."""
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    rules = _rules()
    text = text.replace(_M, '')
    for secret in sorted({s for s in secrets if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True):
        for form in {secret, quote(secret, safe=''), quote_plus(secret)}:
            if form in text:
                text = text.replace(form, _M)
    text = rules['userinfo'].sub(lambda m: m.group(1) + _M + '@', text)
    text = rules['header'].sub(lambda m: m.group(1) + m.group(2) + ((m.group(3) + ' ') if m.group(3) else '') + _M, text)
    text = rules['bearer'].sub(lambda m: m.group(1) + ' ' + _M, text)
    text = rules['xml_setting'].sub(lambda m: m.group(1) + _M + m.group(3), text)
    text = rules['xml_element'].sub(lambda m: m.group(1) + _M + m.group(4), text)
    text = rules['xml_attr'].sub(lambda m: m.group(1) + m.group(2) + _M + m.group(2), text)
    text = rules['field'].sub(lambda m: _plain(m, m.group(2)) or (m.group(1) + m.group(2) + m.group(1) + m.group(3) + m.group(4) + _M + m.group(4)), text)
    text = rules['field_bare'].sub(lambda m: _plain(m, m.group(2)) or (m.group(1) + m.group(2) + m.group(1) + m.group(3) + _M), text)
    text = rules['encoded_kv'].sub(lambda m: m.group(1) + '%3D' + _M, text)
    text = rules['strong_kv'].sub(lambda m: _plain(m, m.group(1)) or (m.group(1) + '=' + _M), text)
    text = rules['weak_query'].sub(lambda m: m.group(1) + '=' + _M, text)
    text = rules['jwt'].sub(_M, text)
    text = rules['dotted'].sub(_M, text)
    text = rules['url'].sub(lambda m: _clean_url(m, rules, strict), text)
    return text.replace(_M, MASK)


def safe_url(url):
    """host/path of a URL for a log line: never the scheme user, the query or a token-like path part."""
    try:
        text = clean(str(url))
        rest = text.partition('://')[2] or text
        rest = rest.partition('?')[0].partition('#')[0]
        return rest.rsplit('@', 1)[-1]
    except Exception:
        return 'url'


_SECRET_ID = re.compile(r'token|secret|passw|refresh|api_?key|session_id|client_id|\.key$', re.I)
_NOT_SECRET_ID = re.compile(r'expires|refresh_widgets|sync_refresh|interval|enabled?$', re.I)


def known_secrets(table):
    """The values of the sign-in settings in POV's settings table (a dict of id -> value). Used only to mask them."""
    found = set()
    try:
        for key, value in dict(table or {}).items():
            key, value = str(key), str(value)
            if len(value) >= 6 and _SECRET_ID.search(key) and not _NOT_SECRET_ID.search(key) and value.lower() not in ('false', 'empty_setting'):
                found.add(value)
    except Exception:
        pass
    return found
