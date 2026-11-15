"""Choose full-source TMDb backdrops only when opening a details page.

This changes a known CDN size component, without fetching or enlarging an image.
An original may be below 4K; its actual dimensions decide the available detail.
Other providers and browse artwork are left as supplied.
"""
import re
from urllib.parse import urlsplit, urlunsplit

_TMDB_BACKDROP = re.compile(r'/t/p/(?:w[1-9][0-9]{0,4}|h[1-9][0-9]{0,4}|original)/([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:jpg|jpeg|png|webp))', re.IGNORECASE)


def detail_backdrop(url):
    if not isinstance(url, str) or not url:
        return ''
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    if parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != 'image.tmdb.org':
        return url
    match = _TMDB_BACKDROP.fullmatch(parsed.path)
    if not match:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, '/t/p/original/' + match[1], parsed.query, parsed.fragment))
