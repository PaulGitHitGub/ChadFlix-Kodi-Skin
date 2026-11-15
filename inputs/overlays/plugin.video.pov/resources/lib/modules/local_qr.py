"""ChadFlix R10.25 (security audit F-03): sign-in QR codes made on the device, and a check of every sign-in link.

Before: the link (and for Trakt the code) of every sign-in went to a third-party QR image service, and the TMDb link
went through a plain-HTTP link shortener. Now nothing about a sign-in leaves the device except to the service itself.

    official(service, url)          the url, or AuthLinkError when it is not https on one of the service's own hosts
    icon(service, url, ...)         path of a local PNG with the QR code of that url ('' when it cannot be drawn:
                                    the screen then shows the link and code as text only, as before)
    cleanup()                       remove every QR file this run made (called when a sign-in ends, whatever the way)

The QR matrix comes from script.module.qrcode (declared in POV's addon.xml); the PNG is written here with zlib, so the
Python Imaging Library is not needed. Files go to special://temp/pov_qr/ with a random name and owner-only access.
"""
import os
import struct
import zlib
from time import time
from urllib.parse import urlsplit

QR_DIR = 'special://temp/pov_qr/'
SIZE = 256              # pixels, as the remote images were
STALE_SECONDS = 1800    # a file left behind by a crash is removed at the next sign-in
_made = []

# The only hosts a sign-in link may point at, per service (exact names, https only).
HOSTS = {
    'premiumize': ('www.premiumize.me', 'premiumize.me'),
    'offcloud': ('offcloud.com', 'www.offcloud.com'),
    'torbox': ('torbox.app', 'www.torbox.app', 'tor.box', 'www.tor.box'),
    'realdebrid': ('real-debrid.com', 'www.real-debrid.com', 'app.real-debrid.com'),
    'alldebrid': ('alldebrid.com', 'www.alldebrid.com'),
    'trakt': ('trakt.tv', 'www.trakt.tv', 'app.trakt.tv'),
    'mdblist': ('mdblist.com', 'www.mdblist.com', 'api.mdblist.com'),
    'tmdb': ('www.themoviedb.org',),
}


class AuthLinkError(Exception):
    """A sign-in link that is not an https link to the service's own site. Carries the service name only."""


def official(service, url):
    """`url` unchanged when it is an https link to one of the service's hosts with no user/password part."""
    try:
        text = str(url)
        parts = urlsplit(text)
        host, port = parts.hostname, parts.port
        fine = (parts.scheme == 'https' and host in HOSTS[service] and parts.username is None and parts.password is None
                and '@' not in parts.netloc and port in (None, 443) and len(text) <= 2048
                and not any(ord(c) <= 32 or ord(c) == 127 for c in text))
    except Exception:
        fine = False
    if not fine:
        raise AuthLinkError(service)
    return text


def _matrix(data):
    """The QR code of `data` as rows of booleans (True = dark), no quiet zone."""
    import qrcode
    code = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_L)
    code.add_data(data)
    code.make(fit=True)
    return [[bool(cell) for cell in row] for row in code.modules]


def _rgb(colour, default):
    try:
        colour = colour.strip('#')
        if len(colour) == 3:
            colour = ''.join(c * 2 for c in colour)
        return bytes(int(colour[i:i + 2], 16) for i in (0, 2, 4)) if len(colour) == 6 else default
    except (AttributeError, ValueError):
        return default


def png(matrix, dark=b'\x00\x00\x00', light=b'\xff\xff\xff', size=SIZE, border=1):
    """An RGB PNG (bytes) of `matrix`, centred on a size x size square of the light colour, 1 module of quiet zone."""
    count = len(matrix) + 2 * border
    scale = max(6, size // count)        # at least 6 px per module, so a long link (a dense code) still scans from a TV
    side = max(size, count * scale)
    pad = (side - count * scale) // 2
    blank = light * side
    rows = []
    for line in matrix:
        row = light * (pad + border * scale) + b''.join((dark if cell else light) * scale for cell in line)
        row += light * (side - len(row) // 3)
        rows.append(b'\x00' + row)
    body = (b'\x00' + blank) * (pad + border * scale) + b''.join(row * scale for row in rows)
    body += (b'\x00' + blank) * (side - (pad + border * scale) - len(matrix) * scale)

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', side, side, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(body, 9)) + chunk(b'IEND', b''))


def _folder():
    from modules import kodi_utils
    return kodi_utils.translate_path(QR_DIR)


def _sweep(folder):
    """Remove QR files older than STALE_SECONDS (left by a crash or a killed Kodi)."""
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if name.startswith('qr-') and name.endswith('.png') and time() - os.stat(path).st_mtime > STALE_SECONDS:
                os.unlink(path)
    except OSError:
        pass


def startup_sweep():
    """At Kodi start no sign-in is open: every QR file still in the folder is a leftover and goes."""
    try:
        folder = _folder()
        for name in os.listdir(folder):
            if name.startswith('qr-') and name.endswith('.png'):
                os.unlink(os.path.join(folder, name))
    except OSError:
        pass


def icon(service, url, color=None, bgcolor=None):
    """Path of a local QR image for a sign-in link, or '' when it cannot be drawn. The link is checked first:
    AuthLinkError when it is not the service's own https link (never drawn, never sent anywhere)."""
    if not url:
        return ''
    official(service, url)
    try:
        folder = _folder()
        os.makedirs(folder, exist_ok=True)
        _sweep(folder)
        data = png(_matrix(url), _rgb(color, b'\x00\x00\x00'), _rgb(bgcolor, b'\xff\xff\xff'))
        path = os.path.join(folder, 'qr-%s-%s.png' % (service, os.urandom(8).hex()))
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
        _made.append(path)
        return path
    except AuthLinkError:
        raise
    except Exception:
        return ''   # no QR module, no temp folder, ...: the link and code are still shown as text


def _forget_texture(path):
    """Ask Kodi to drop its cached copy of the image (best effort; the code in it has expired by then anyway)."""
    try:
        import json
        import xbmc
        found = json.loads(xbmc.executeJSONRPC(json.dumps({
            'jsonrpc': '2.0', 'id': 1, 'method': 'Textures.GetTextures',
            'params': {'filter': {'field': 'url', 'operator': 'contains', 'value': os.path.basename(path)}}})))
        for texture in found.get('result', {}).get('textures', []):
            xbmc.executeJSONRPC(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'Textures.RemoveTexture',
                                            'params': {'textureid': texture['textureid']}}))
    except Exception:
        pass


def cleanup():
    """Delete every QR file made in this run. Safe to call at any time, any number of times."""
    while _made:
        path = _made.pop()
        try:
            os.unlink(path)
        except OSError:
            pass
        _forget_texture(path)
