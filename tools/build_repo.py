#!/usr/bin/env python3
"""Build the ChadFlix Kodi add-on repository (the GitHub Pages tree) from the release snapshot.

    python3 tools/build_repo.py                      # build into this folder (the published tree)
    python3 tools/build_repo.py --base-url http://127.0.0.1:8765/ --out /some/test-site
                                                     # same build, repository pointed at a local test server

Inputs (paths in config/repo.json, relative to the private working folder two levels up):
  - full add-on folders: the Shield pull (skin, POV and the jurialmunkey add-ons, as on the device) and,
    for add-ons not in the pull, the Kodi 21.3 test copy's add-on folders;
  - untracked skin assets (images) from the staged release packages, in release order;
  - the release snapshot (only the tracked files), applied last, so tracked files equal the snapshot;
  - the Shield's skin layout settings (display options only) and the snapshot's four keymaps, which go
    into the setup add-on.

Output (all regenerated on every run; old zips are removed):
  index.html                          Pages root, plain <a href> links Kodi's "Add source" browser can list
  repository.chadflix-<v>.zip        the repository add-on, for "Install from zip file"
  zips/addons.xml, addons.xml.md5     the repository index
  zips/<id>/<id>-<version>.zip        one zip per add-on, zip root = the add-on id folder
  zips/<id>/<icon/fanart>             artwork Kodi shows while browsing the repository

Patched add-ons get version  <upstream major>.99.<other upstream digits>.<btn_revision>
(POV 6.11.03 -> 6.99.1103.1, Arctic Fuse 3 3.4.18 -> 3.99.418.1). That stays above every upstream 6.x / 3.x
release, so an upstream update never replaces the patched files, and keeps the major number POV puts in its
User-Agent ("POV/6.0").

The build refuses to publish anything private: it stops on settings / database / log / bytecode files, on
the owner's name, home paths or LAN addresses, and writes a full report of every credential-like word
(token, api_key, password, secret, refresh, @ ...) to ../kodi-repo-evidence/privacy-scan.txt.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import zipfile
import zlib
from xml.sax.saxutils import escape

PUB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KPH = os.path.dirname(os.path.dirname(PUB))
CFG = json.load(open(os.path.join(PUB, 'config', 'repo.json'), encoding='utf-8'))
INP = {k: (os.path.join(KPH, v) if isinstance(v, str) else [os.path.join(KPH, x) for x in v])
       for k, v in CFG['inputs'].items()}
UPSTREAM_REF = os.path.join(KPH, 'publish', 'upstream-ref')
EVIDENCE = os.path.join(KPH, 'publish', 'kodi-repo-evidence')

SKIP_DIRS = {'__pycache__', '.git', '.svn'}
SKIP_FILES = {'.DS_Store', 'Thumbs.db'}
# Release-tracked files (tools/release): these must equal the snapshot after the build.
TRACKED = [r'^skin\.arctic\.fuse\.3/1080i/[^/]+\.xml$', r'^skin\.arctic\.fuse\.3/extras/[^/]+\.py$',
           r'^[^/]+/addon\.xml$', r'^plugin\.video\.pov/resources/lib/.*\.py$',
           r'^plugin\.video\.pov/resources/skins/.*\.xml$']
# Private text that must never ship (owner's name, home paths, LAN addresses) and the docstring redactions
# live in config/private-patterns.local.json, which is git-ignored so the patterns themselves stay private.
_LOCAL = os.path.join(PUB, 'config', 'private-patterns.local.json')
if os.path.isfile(_LOCAL):
    _P = json.load(open(_LOCAL, encoding='utf-8'))
else:
    print('WARNING: config/private-patterns.local.json missing: only generic privacy patterns are checked')
    _P = {'hard_patterns': [r'192\.168\.', r'/U[s]ers/', r'(?i)/h[o]me/[a-z]'], 'hard_allow': [], 'redact': []}
HARD = _P['hard_patterns']
HARD_ALLOW = set(_P['hard_allow'])  # upstream text checked byte-identical to the author's release
REDACT = [tuple(r) for r in _P['redact']]
REPORT = ['token', 'apikey', 'api_key', 'password', 'secret', 'client_secret', 'refresh', '@']
# Static reference data that ships inside TMDb Helper's own package (award and collection lists, no user data).
STOCK_DATA = {'plugin.video.themoviedb.helper/resources/databases/awards.db',
              'plugin.video.themoviedb.helper/resources/databases/collections.db'}
TEXT_EXT = {'.py', '.xml', '.txt', '.md', '.po', '.json', '.html', '.htm', '.css', '.js', '.cfg', '.ini', '.sh',
            '.in', '.yml', '.yaml'}


def die(msg):
    sys.exit('BUILD STOPPED: ' + msg)


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def walk(root):
    """Relative file paths under root, skipping bytecode and OS junk."""
    out = []
    for r, ds, fs in os.walk(root):
        ds[:] = sorted(d for d in ds if d not in SKIP_DIRS)
        for f in sorted(fs):
            if f in SKIP_FILES or f.endswith(('.pyc', '.pyo')):
                continue
            out.append(os.path.relpath(os.path.join(r, f), root))
    return out


def copy_tree(src, dst):
    for rel in walk(src):
        d = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copyfile(os.path.join(src, rel), d)


def is_tracked(rel):
    return any(re.match(p, rel) for p in TRACKED)


def addon_tag(xml):
    return re.search(r'<addon\b[^>]*>', xml, re.S).group(0)


def get_version(xml):
    return re.search(r'\bversion="([^"]+)"', addon_tag(xml)).group(1)


def set_version(xml, version):
    tag = addon_tag(xml)
    return xml.replace(tag, re.sub(r'\bversion="[^"]+"', 'version="%s"' % version, tag), 1)


def btn_version(upstream):
    parts = re.split(r'[.+~]', upstream)
    rest = ''.join(p for p in parts[1:] if p.isdigit())
    return '%s.99.%s.%d' % (parts[0], rest or '0', CFG['btn_revision'])


def base_url(args):
    if args.base_url:
        return args.base_url if args.base_url.endswith('/') else args.base_url + '/'
    return 'https://%s.github.io/%s/' % (CFG['github_user'].lower(), CFG['repo_slug'])


def source_url():
    return 'https://github.com/%s/%s' % (CFG['github_user'], CFG['repo_slug'])


# ------------------------------------------------------------------------------------------------ assemble
def assemble_addon(addon_id, work):
    src = os.path.join(INP['shield_pull'], addon_id)
    origin = 'Shield pull'
    if not os.path.isfile(os.path.join(src, 'addon.xml')):
        src = os.path.join(INP['addon_folders'], addon_id)
        origin = 'Kodi 21.3 test copy'
    if not os.path.isfile(os.path.join(src, 'addon.xml')):
        die('no source folder for %s' % addon_id)
    dst = os.path.join(work, addon_id)
    copy_tree(src, dst)
    if addon_id not in CFG['patched']:
        return dst, origin
    # untracked assets (images) from the staged packages, in release order
    for ov in INP['asset_overlays']:
        base = os.path.join(ov, 'addons', addon_id)
        if not os.path.isdir(base):
            continue
        for rel in walk(base):
            full = addon_id + '/' + rel
            if is_tracked(full):
                continue
            d = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(os.path.join(base, rel), d)
    # the snapshot, last
    snap = os.path.join(INP['snapshot'], 'files', 'addons', addon_id)
    copy_tree(snap, dst)
    # the snapshot is the whole truth for tracked files: a tracked file it lacks was deleted by a later release
    # (R9.3, R9.4, R10.4 ...) but may still be in the older add-on folders above, so remove it (listed in the output)
    man = json.load(open(os.path.join(INP['snapshot'], 'manifest.json'), encoding='utf-8'))['files']
    for rel in walk(dst):
        full = addon_id + '/' + rel
        if is_tracked(full) and ('addons/' + full) not in man:
            os.remove(os.path.join(dst, rel))
            DELETED.append(full)
            print('removed (not in snapshot): %s' % full)
    return dst, origin + ' + staged assets + snapshot'


DELETED = []


def verify_snapshot(work):
    man = json.load(open(os.path.join(INP['snapshot'], 'manifest.json'), encoding='utf-8'))['files']
    bad = []
    n = 0
    for key, want in man.items():
        if not key.startswith('addons/'):
            continue
        rel = key[len('addons/'):]
        if rel.split('/')[0] not in CFG['patched']:
            continue
        n += 1
        p = os.path.join(work, rel)
        if not os.path.isfile(p) or sha256(p) != want:
            bad.append(rel)
    # every tracked-type file in the patched add-ons must be in the manifest (no stale extras)
    extra = []
    for a in CFG['patched']:
        for rel in walk(os.path.join(work, a)):
            full = a + '/' + rel
            if is_tracked(full) and ('addons/' + full) not in man:
                extra.append(full)
    if bad or extra:
        die('snapshot mismatch: %s / not in snapshot: %s' % (bad[:10], extra[:10]))
    return n


def verify_skin_images(skin_dir):
    refs = set()
    for rel in walk(skin_dir):
        if rel.endswith(('.xml', '.py')):
            t = open(os.path.join(skin_dir, rel), encoding='utf-8', errors='replace').read()
            refs |= set(re.findall(r'special://skin/(extras/[A-Za-z0-9_./-]+\.(?:png|jpg|gif))', t))
    # pink_blur.jpg (backgrounds/) is referenced but absent in upstream 3.4.18 too
    missing = sorted(r for r in refs if not os.path.exists(os.path.join(skin_dir, r))
                     and r != 'extras/backgrounds/pink_blur.jpg')
    if missing:
        die('skin references missing images: %s' % missing)
    return len(refs)


def apply_redactions(work):
    for rel, old, new in REDACT:
        p = os.path.join(work, rel)
        t = open(p, encoding='utf-8').read()
        if old not in t:
            die('redaction text not found in %s (snapshot changed? update REDACT)' % rel)
        open(p, 'w', encoding='utf-8').write(t.replace(old, new))


def mark_patched(addon_dir, addon_id, upstream_version, new_version):
    """Version bump, a visible 'modified' notice (GPL-3 s.5a / CC BY-NC-SA 'indicate changes'), change list."""
    p = os.path.join(addon_dir, 'addon.xml')
    xml = open(p, encoding='utf-8').read()
    xml = set_version(xml, new_version)
    note = ('[CR][CR]UNOFFICIAL MODIFIED BUILD %s (ChadFlix, 2026-11), based on %s %s. '
            'Not made or supported by the original author. See CHADFLIX-CHANGES.txt.' % (new_version, addon_id, upstream_version))
    m = re.search(r'(<description lang="en(?:_GB)?">)(.*?)(</description>)', xml, re.S)
    if m:
        xml = xml.replace(m.group(0), m.group(1) + m.group(2) + escape(note) + m.group(3), 1)
    else:
        die('no English description in %s' % addon_id)
    open(p, 'w', encoding='utf-8').write(xml)
    ref = os.path.join(UPSTREAM_REF, addon_id)
    lines = ['%s %s: unofficial modified build of %s %s' % (addon_id, new_version, addon_id, upstream_version),
             'Modified by ChadFlix, 2026-11 (unofficial modified build for Kodi 21).',
             'Original author and copyright: see provider-name in addon.xml. Licence: see LICENSE.txt (or the <license> tag in addon.xml).', '']
    if os.path.isdir(ref):
        up = set(walk(ref))
        ours = set(walk(addon_dir))
        changed = sorted(r for r in ours & up if r != 'addon.xml' and sha256(os.path.join(ref, r)) != sha256(os.path.join(addon_dir, r)))
        added = sorted(r for r in ours - up if r != 'CHADFLIX-CHANGES.txt')
        removed = sorted(up - ours)
        lines += ['Compared with the unmodified upstream %s %s:' % (addon_id, upstream_version),
                  'Changed files (%d):' % len(changed)] + ['  ' + r for r in changed]
        lines += ['Added files (%d):' % len(added)] + ['  ' + r for r in added]
        lines += ['Removed files (%d):' % len(removed)] + ['  ' + r for r in removed]
        lines += ['addon.xml: version changed to %s and this notice added to the description.' % new_version]
    open(os.path.join(addon_dir, 'CHADFLIX-CHANGES.txt'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')


# --------------------------------------------------------------------------------------- our own add-ons
def png_icon(path, size=256):
    """A plain icon (dark tile, teal ring open on the right, like a "C") without image libraries."""
    import math
    bg, fg = (24, 26, 32), (38, 198, 218)
    c = (size - 1) / 2.0
    r_out, r_in = size * 0.36, size * 0.24
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = math.hypot(x - c, y - c)
            ang = math.degrees(math.atan2(y - c, x - c))
            on = r_in <= d <= r_out and not (-40 <= ang <= 40)
            row += bytes(fg if on else bg)
        rows.append(bytes(row))
    raw = zlib.compress(b''.join(rows), 9)

    def chunk(t, d):
        c2 = struct.pack('>I', len(d)) + t + d
        return c2 + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', raw) + chunk(b'IEND', b'')
    open(path, 'wb').write(data)


def build_repository_addon(work, url):
    rc = CFG['repository']
    d = os.path.join(work, rc['id'])
    os.makedirs(d)
    t = open(os.path.join(PUB, 'src', rc['id'], 'addon.xml.in'), encoding='utf-8').read()
    t = t.replace('{ID}', rc['id']).replace('{NAME}', rc['name']).replace('{VERSION}', rc['version']) \
         .replace('{BASE_URL}', url).replace('{SOURCE_URL}', source_url())
    open(os.path.join(d, 'addon.xml'), 'w', encoding='utf-8').write(t)
    shutil.copyfile(os.path.join(PUB, 'src', rc['id'], 'LICENSE.txt'), os.path.join(d, 'LICENSE.txt'))
    png_icon(os.path.join(d, 'icon.png'))
    return d


def build_setup_addon(work, versions):
    sc = CFG['setup']
    src = os.path.join(PUB, 'src', sc['id'])
    d = os.path.join(work, sc['id'])
    os.makedirs(os.path.join(d, 'resources', 'keymaps'))
    shutil.copyfile(os.path.join(src, 'default.py'), os.path.join(d, 'default.py'))
    shutil.copyfile(os.path.join(src, 'service.py'), os.path.join(d, 'service.py'))
    shutil.copyfile(os.path.join(src, 'LICENSE.txt'), os.path.join(d, 'LICENSE.txt'))
    png_icon(os.path.join(d, 'icon.png'))
    for k in CFG['keymaps']:
        shutil.copyfile(os.path.join(INP['snapshot'], 'files', 'userdata', 'keymaps', k),
                        os.path.join(d, 'resources', 'keymaps', k))
    # skin layout settings: display options only (same guard as the Mac zip's make_skin_settings.py)
    s = json.load(open(INP['skin_settings'], encoding='utf-8'))
    bools = list(dict.fromkeys(s['bools'] + ['Netflix.TabSwap', 'DefaultConfig.InitDone']))
    strings = {k: v for k, v in s['strings'].items() if k not in CFG['skin_settings_drop']}
    strings.update(CFG.get('skin_settings_extra', {}))
    suspect = re.compile(r'(key|token|secret|pass|user|login|mail|account|api|auth|city|zip|lat|lon)', re.I)
    bad = [k for k in bools + list(strings) if suspect.search(k)]
    if bad:
        die('skin settings ids look sensitive: %s' % bad)
    reqs = []
    for a in CFG['setup_requires']:
        v = versions.get(a) or CFG['official_versions'].get(a, '')
        reqs.append([a, v])
    # hashes of key files that earlier Setup versions shipped: the service replaces only these (never a file the
    # person changed), and Undo removes them like the current ones
    prev = CFG.get('keymaps_previous', {})
    for k in CFG['keymaps']:
        cur = sha256(os.path.join(d, 'resources', 'keymaps', k))
        if cur in prev.get(k, []):
            die('keymaps_previous lists the CURRENT %s hash' % k)
    json.dump({'skin': 'skin.arctic.fuse.3', 'requires': reqs, 'keymaps': CFG['keymaps'], 'keymaps_previous': prev,
               'skin_bools': bools, 'skin_strings': strings},
              open(os.path.join(d, 'resources', 'setup.json'), 'w', encoding='utf-8'), indent=1)
    imports = '\n'.join('        <import addon="%s" version="%s" />' % (a, v) if v else
                        '        <import addon="%s" />' % a for a, v in reqs)
    t = open(os.path.join(src, 'addon.xml.in'), encoding='utf-8').read()
    t = t.replace('{ID}', sc['id']).replace('{NAME}', sc['name']).replace('{VERSION}', sc['version']) \
         .replace('{REQUIRES}', imports).replace('{SOURCE_URL}', source_url())
    open(os.path.join(d, 'addon.xml'), 'w', encoding='utf-8').write(t)
    return d


# ------------------------------------------------------------------------------------------- privacy scan
def privacy_scan(work, ids, report_path):
    bad_names = []
    for a in ids:
        for rel in walk(os.path.join(work, a)):
            low = rel.lower()
            name = os.path.basename(low)
            if (a + '/' + rel) in STOCK_DATA:
                continue
            if ('addon_data' in low or low.endswith(('.db', '.log', '.sqlite', '.bak', '.orig', '.rej'))
                    or name in ('kodi.log', 'kodi.old.log', 'passwords.xml', 'sources.xml', 'profiles.xml',
                                'guisettings.xml', 'favourites.xml', 'advancedsettings.xml')
                    or (os.path.basename(rel) == 'settings.xml' and rel != 'resources/settings.xml')
                    or 'netflix-style-backup' in low or 'evidence' in low.split('/')):
                bad_names.append(a + '/' + rel)
    if bad_names:
        die('private-looking files in the tree: %s' % bad_names[:20])
    hard = [re.compile(h) for h in HARD]
    rep = re.compile('|'.join(re.escape(p) for p in REPORT), re.I)
    mask = re.compile(r"""(['"])[A-Za-z0-9_\-]{16,}(['"])""")
    hard_hits, counts, detail = [], {}, []
    for a in ids:
        root = os.path.join(work, a)
        ref = os.path.join(UPSTREAM_REF, a)
        for rel in walk(root):
            if os.path.splitext(rel)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.join(root, rel)
            text = open(p, encoding='utf-8', errors='replace').read()
            for i, line in enumerate(text.splitlines(), 1):
                if any(h.search(line) for h in hard) and ('%s/%s:%d' % (a, rel, i)) not in HARD_ALLOW:
                    hard_hits.append('%s/%s:%d' % (a, rel, i))
            hits = [(i, l) for i, l in enumerate(text.splitlines(), 1) if rep.search(l)]
            if not hits:
                continue
            if a in CFG['patched']:
                r = os.path.join(ref, rel)
                state = 'upstream-identical' if os.path.isfile(r) and sha256(r) == sha256(p) else 'MODIFIED-OR-ADDED'
            elif a in (CFG['setup']['id'], CFG['repository']['id']):
                state = 'OURS'
            else:
                state = 'unmodified-addon'
            counts.setdefault((a, state), 0)
            counts[(a, state)] += len(hits)
            if state in ('MODIFIED-OR-ADDED', 'OURS'):
                for i, l in hits:
                    detail.append('%s/%s:%d: %s' % (a, rel, i, mask.sub(r'\1<masked>\2', l.strip())[:180]))
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write('Privacy scan of the add-on folders before zipping\n')
        f.write('Hard patterns (build stops on any): %s\n' % ', '.join(HARD))
        f.write('Hard hits: %d\n\n' % len(hard_hits))
        f.write('Credential-like words (%s), hit lines per add-on:\n' % ', '.join(REPORT))
        for (a, s), n in sorted(counts.items()):
            f.write('  %-45s %-20s %5d\n' % (a, s, n))
        f.write('\nEvery hit line in modified/added/our files (string values of 16+ chars masked):\n')
        f.write('\n'.join(detail) + '\n')
    if hard_hits:
        die('private text found: %s' % hard_hits[:20])
    return counts, len(detail)


def scan_published_tree(out):
    """Hard patterns over every text file of the published tree (README, NOTICE, CHANGELOG, index.html, tools,
    config, src, addons.xml): everything that git tracks except the zips, whose contents were scanned above."""
    hard = [re.compile(h) for h in HARD]
    hits, n = [], 0
    for rel in walk(out):
        if rel.endswith('.zip') or rel.startswith('.git/') or rel == 'config/private-patterns.local.json':
            continue
        p = os.path.join(out, rel)
        try:
            text = open(p, encoding='utf-8').read()
        except UnicodeDecodeError:
            continue  # binary (icons)
        n += 1
        for i, line in enumerate(text.splitlines(), 1):
            if any(h.search(line) for h in hard):
                hits.append('%s:%d' % (rel, i))
    with open(os.path.join(EVIDENCE, 'privacy-scan.txt'), 'a', encoding='utf-8') as f:
        f.write('\nPublished tree (all non-zip files, %d text files): %d hard hits %s\n' % (n, len(hits), hits))
    if hits:
        die('private text in the published tree: %s' % hits[:20])
    return n


# ------------------------------------------------------------------------------------------------ publish
def zip_addon(addon_dir, addon_id, out_zip):
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        dirs = set()
        for rel in walk(addon_dir):
            parts = rel.split('/')[:-1]
            for i in range(len(parts) + 1):
                dname = '/'.join([addon_id] + parts[:i]) + '/'
                if dname not in dirs:
                    dirs.add(dname)
                    zi = zipfile.ZipInfo(dname, date_time=(2026, 11, 12, 0, 0, 0))
                    zi.external_attr = (0o40755 << 16) | 0x10
                    z.writestr(zi, b'')
            zi = zipfile.ZipInfo(addon_id + '/' + rel, date_time=(2026, 11, 12, 0, 0, 0))
            zi.external_attr = 0o644 << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(addon_dir, rel), 'rb') as f:
                z.writestr(zi, f.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def asset_paths(xml):
    m = re.search(r'<assets>(.*?)</assets>', xml, re.S)
    if not m:
        return []
    return [p.strip() for p in re.findall(r'<(?:icon|fanart)>([^<]+)</', m.group(1))]


INDEX_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'index.html.in')
INDEX_TEMPLATE_ZIP = 'repository.chadflix-1.0.0.zip'   # the names written in index.html.in, swapped for the real ones


def write_index(out, repo_zip, url):
    """The showcase page (tools/index.html.in) when present, else the plain source page. Either way the page keeps
    one link written exactly <a href="ZIP">ZIP</a>, which is what Kodi's "Add source" browser lists."""
    if os.path.isfile(INDEX_TEMPLATE):
        html = open(INDEX_TEMPLATE, encoding='utf-8').read().replace(INDEX_TEMPLATE_ZIP, repo_zip)
        kodi_link = '<a href="%s">%s</a>' % (repo_zip, repo_zip)
        if html.count(kodi_link) != 1:
            die('tools/index.html.in must contain exactly one %s (Kodi lists only that form)' % kodi_link)
        if url.lower().rstrip('/') not in html.lower():
            print('note: index.html shows the Pages address, not %s (fine for a local test build)' % url)
        open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(html)
        return
    rc = CFG['repository']
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ChadFlix Kodi source</title>
<style>body{{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;background:#181a20;color:#eee}}a{{color:#26c6da}}code{{background:#222;padding:.1rem .3rem}}</style>
</head>
<body>
<h1>ChadFlix Kodi source</h1>
<p>This page is a Kodi file source. In Kodi: <b>Add-ons &rarr; Install from zip file</b> &rarr; pick this source &rarr; pick the file below.
Full instructions: <a href="{src}">the GitHub page</a></p>
<ul>
<li><a href="{z}">{z}</a></li>
</ul>
<p>Source URL to type in Kodi's File manager: <code>{url}</code></p>
</body>
</html>
""".format(z=repo_zip, url=url, src=source_url())
    open(os.path.join(out, 'index.html'), 'w', encoding='utf-8').write(html)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base-url', help='override the Pages URL (for a local test server)')
    ap.add_argument('--out', default=PUB, help='where to write index.html, the repository zip and zips/')
    ap.add_argument('--work', default=os.path.join(KPH, 'publish', '.build'), help='scratch folder (wiped)')
    args = ap.parse_args()
    url = base_url(args)
    out = os.path.abspath(args.out)
    work = os.path.abspath(args.work)
    if os.path.commonpath([work, PUB]) == PUB:
        die('--work must be outside the published tree')
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    os.makedirs(EVIDENCE, exist_ok=True)

    versions, origins = {}, {}
    for a in CFG['hosted']:
        d, origins[a] = assemble_addon(a, work)
    # the removals must be exactly the known deletions (repo.json "expected_removed"): a new deletion, or a file the
    # snapshot gained back, has to be looked at and listed before the build goes on
    want = sorted(CFG.get('expected_removed', []))
    if sorted(DELETED) != want:
        die('removed tracked files differ from expected_removed: unexpected %s / expected but not removed %s'
            % (sorted(set(DELETED) - set(want)), sorted(set(want) - set(DELETED))))
    n_tracked = verify_snapshot(work)
    n_img = verify_skin_images(os.path.join(work, 'skin.arctic.fuse.3'))
    apply_redactions(work)
    pov_settings = os.path.join(work, 'plugin.video.pov', 'resources', 'settings.xml')
    if sha256(pov_settings) != CFG['stock_pov_settings_sha256']:
        die('POV resources/settings.xml (holds POV\'s bundled default ids) is not the stock file')
    for a in CFG['hosted']:
        xml = open(os.path.join(work, a, 'addon.xml'), encoding='utf-8').read()
        up = get_version(xml)
        if a in CFG['patched']:
            versions[a] = btn_version(up)
            mark_patched(os.path.join(work, a), a, up, versions[a])
        else:
            versions[a] = up
    # GPL-3 text next to POV (its upstream package has none)
    if not any(f.lower().startswith(('license', 'copying')) for f in os.listdir(os.path.join(work, 'plugin.video.pov'))):
        shutil.copyfile(os.path.join(PUB, 'LICENSES', 'GPL-3.0.txt'), os.path.join(work, 'plugin.video.pov', 'LICENSE.txt'))
    build_setup_addon(work, versions)
    build_repository_addon(work, url)
    sc, rc = CFG['setup'], CFG['repository']
    versions[sc['id']] = sc['version']
    versions[rc['id']] = rc['version']
    ids = CFG['hosted'] + [sc['id'], rc['id']]

    counts, n_detail = privacy_scan(work, ids, os.path.join(EVIDENCE, 'privacy-scan.txt'))

    # publish
    zips = os.path.join(out, 'zips')
    shutil.rmtree(zips, ignore_errors=True)
    for f in os.listdir(out) if os.path.isdir(out) else []:
        if f.startswith(rc['id'] + '-') and f.endswith('.zip'):
            os.remove(os.path.join(out, f))
    os.makedirs(zips)
    parts = []
    sizes = []
    for a in ids:
        d = os.path.join(work, a)
        xml = open(os.path.join(d, 'addon.xml'), encoding='utf-8').read()
        zd = os.path.join(zips, a)
        os.makedirs(zd)
        zp = os.path.join(zd, '%s-%s.zip' % (a, versions[a]))
        zip_addon(d, a, zp)
        for asset in asset_paths(xml):
            s = os.path.join(d, asset)
            if os.path.isfile(s):
                os.makedirs(os.path.dirname(os.path.join(zd, asset)), exist_ok=True)
                shutil.copyfile(s, os.path.join(zd, asset))
        parts.append(re.sub(r'^\s*<\?xml[^>]*\?>\s*', '', xml).strip())
        sizes.append((a, versions[a], os.path.getsize(zp), origins.get(a, 'built from src/')))
        if os.path.getsize(zp) > 95 * 1024 * 1024:
            die('%s is over 95 MB (GitHub limit is 100 MB per file)' % zp)
    addons_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n' + '\n'.join(parts) + '\n</addons>\n'
    open(os.path.join(zips, 'addons.xml'), 'w', encoding='utf-8').write(addons_xml)
    open(os.path.join(zips, 'addons.xml.md5'), 'w').write(hashlib.md5(addons_xml.encode('utf-8')).hexdigest())
    repo_zip = '%s-%s.zip' % (rc['id'], rc['version'])
    shutil.copyfile(os.path.join(zips, rc['id'], repo_zip), os.path.join(out, repo_zip))
    write_index(out, repo_zip, url)
    if out == PUB:
        open(os.path.join(out, '.nojekyll'), 'w').close()

    n_tree = scan_published_tree(out)

    print('repository URL: %s' % url)
    print('published-tree scan: %d text files outside the zips checked against the hard patterns: 0 hits' % n_tree)
    print('snapshot files verified: %d; skin image references resolved: %d' % (n_tracked, n_img))
    print('tracked files removed because the snapshot no longer has them (%d): %s' % (len(DELETED), DELETED))
    print('privacy scan: no hard hits; %d credential-word lines in modified/our files (see %s)'
          % (n_detail, os.path.join(EVIDENCE, 'privacy-scan.txt')))
    total = 0
    for a, v, s, o in sizes:
        total += s
        print('  %-42s %-14s %7.1f MB  (%s)' % (a, v, s / 1048576.0, o))
    print('total zips: %.1f MB' % (total / 1048576.0))


if __name__ == '__main__':
    main()
