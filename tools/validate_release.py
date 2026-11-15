#!/usr/bin/env python3
"""Release gate for the ChadFlix repository tree (security audit F-01, F-09, F-10). Offline: it opens no network connection.

    python3 tools/validate_release.py SITE --public-only                 what CI runs: every public check
    python3 tools/validate_release.py SITE --private-patterns FILE       the publication gate on the owner's machine
    python3 tools/validate_release.py SITE --public-only --write-baseline   (after review) record today's findings

SITE is a tree made by tools/build_release.py (or the repository root, which holds the same generated files).
Exit code 0 only when every check passes. A check that cannot run counts as failed.

  hashes      addons.xml.sha256 / .md5, every <zip>.sha256 and release-manifest.json equal the bytes on disk
  index       every index entry has exactly its zip; the entry equals the addon.xml packed in the zip; no stray zip
  packages    zip paths (no absolute path, "..", backslash or symbolic link; one top folder named as the add-on)
  repository  the repository add-on verifies SHA-256 for the index and the packages; both copies of its zip are equal
  lock        versions equal config/release.lock.json and are not lower than the previous release; carried zips unchanged
  contents    no bytecode, VCS folder, OS junk, log, database (other than the two pinned TMDb Helper catalogues),
              nested archive, private key or native binary
  secrets     credential-shaped text and private paths / addresses in every text file, and in image metadata.
              Every finding must be in config/scan-allowlist.json (one entry per place and value fingerprint, with a reason).
  private     the owner's private patterns (names, home paths, LAN addresses). Required unless --public-only is given;
              with --public-only this check is reported as NOT RUN and the tree is not cleared for publication.

Reports never print a matched value or a private pattern: a finding is shown as path, line, rule and a fingerprint
(the first 16 hex digits of SHA-256 over rule and value).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_LIMIT = 8 * 1024 * 1024
STOCK_DB = {  # reference catalogues inside TMDb Helper's own package (award and collection lists, no user data)
    'plugin.video.themoviedb.helper/resources/databases/awards.db',
    'plugin.video.themoviedb.helper/resources/databases/collections.db'}
BAD_NAMES = re.compile(r'(^|/)(__pycache__|\.git|\.svn|\.hg)(/|$)|(^|/)(\.DS_Store|Thumbs\.db|guisettings\.xml|sources\.xml|passwords\.xml'
                       r'|advancedsettings\.xml)$|\.(pyc|pyo|log|db|sqlite|sqlite3|bak|orig|rej|pem|key|p12|pfx|zip|apk|so|dylib|dll|exe)$', re.I)
BINARY_MAGIC = (b'\x7fELF', b'MZ', b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe')
NAME = r'[A-Za-z0-9_.\-]*(?:token|secret|passw(?:or)?d|api[_\-]?key|apikey|client[_\-]?secret|refresh[_\-]?token|session[_\-]?id)[A-Za-z0-9_.\-]*'
VALUE = r'[A-Za-z0-9_\-+/=.~%]{12,}'
RULES = [(name, re.compile(rx, flags)) for name, rx, flags in (
    ('private-key', r'-----BEGIN [A-Z ]*PRIVATE KEY-----', 0),
    ('jwt', r'\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-]{8,})?', 0),
    ('dotted-token', r'(?<![A-Za-z0-9_\-.])[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}(?![A-Za-z0-9_\-.])', 0),
    ('bearer', r'\b(?:bearer|basic)\s+(' + VALUE + r')', re.I),
    ('url-credential', r'[?&](?:' + NAME + r'|key|code|pin|auth|sig)=(' + VALUE + r')', re.I),
    ('url-userinfo', r'\b[a-z][a-z0-9+.\-]{1,15}://[^/\s:@"\'<>]+:([^/\s@"\'<>{}$%]{4,})@', re.I),
    ('xml-element', r'<(' + NAME + r')(?:\s[^>]*)?>\s*(' + VALUE + r')\s*</\1>', re.I),
    ('xml-attribute', r'\s(?:' + NAME + r')\s*=\s*"(' + VALUE + r')"', re.I),
    ('xml-setting', r'<setting\b[^>]*\bid="(?:' + NAME + r')"[^>]*\b(?:default|value)="(' + VALUE + r')"', re.I),
    ('assignment', r'''["']?(?:''' + NAME + r''')["']?\s*[:=]\s*["'](''' + VALUE + r''')["']''', re.I),
    # a real home folder; not Kodi's own special://home/ and not a path inside another path
    ('home-path', r'(?<![A-Za-z0-9:/._\-])(?:/Users/|/home/|[A-Z]:\\Users\\)([A-Za-z][A-Za-z0-9._\-]{1,30})(?=[/\\])', 0),
    ('private-address', r'(?<![0-9.])((?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3})(?![0-9.])', 0),
    ('mac-address', r'(?<![0-9A-Fa-f:])((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f:])', 0),
)]
META_RULES = [(name, re.compile(rx, re.I)) for name, rx in (
    ('image-gps', rb'GPSLatitude|GPSLongitude|exif:GPS'),
    ('image-serial', rb'SerialNumber|LensSerial|CameraOwnerName|OwnerName'),
    ('image-email', rb'[A-Za-z0-9._%+\-]{2,}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}'),
    ('image-home-path', rb'(?:/Users/|/home/|[A-Z]:\\Users\\)[A-Za-z][A-Za-z0-9._\-]{1,30}[/\\]'),
)]
# values of these shapes are placeholders or format strings, never a credential
PLACEHOLDER = re.compile(r'^(?:[%{$<\[].*|.*[%{}$]s?.*|x{6,}|X{6,}|[a-z_]+|[A-Z_]+|(?:true|false|none|null|empty_setting)|\d+(?:\.\d+)*)$')


class Gate:
    def __init__(self):
        self.failed, self.lines = {}, []

    def say(self, text):
        self.lines.append(text)
        print(text)

    def check(self, name, problems, not_run=None):
        if not_run:
            self.failed[name] = [not_run]
            self.say('CHECK %-10s NOT RUN: %s' % (name, not_run))
        elif problems:
            self.failed[name] = problems
            self.say('CHECK %-10s FAILED (%d)' % (name, len(problems)))
            for p in problems[:40]:
                self.say('    ' + p)
            if len(problems) > 40:
                self.say('    ... and %d more' % (len(problems) - 40))
        else:
            self.say('CHECK %-10s OK' % name)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def first_word(path):
    return read(path).decode('ascii', 'replace').split()[0] if os.path.isfile(path) and read(path).split() else ''


def fingerprint(rule, value):
    return hashlib.sha256(rule.encode() + b'\0' + (value if isinstance(value, bytes) else value.encode('utf-8', 'replace'))).hexdigest()[:16]


def scan_text(where, data, findings):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return False
    if '\x00' in text:
        return False
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > 20000:
            line = line[:20000]
        for rule, rx in RULES:
            for m in rx.finditer(line):
                value = m.group(m.lastindex or 0)
                if rule in ('bearer', 'url-credential', 'xml-element', 'xml-attribute', 'xml-setting', 'assignment') and PLACEHOLDER.match(value):
                    continue
                if rule == 'bearer' and not (re.search(r'[0-9]', value) and re.search(r'[A-Za-z]', value)):
                    continue        # prose ("basic permissions"), not a header value
                findings.append((where, number, rule, fingerprint(rule, value)))
    return True


def image_metadata(data):
    """The metadata parts of a PNG or JPEG (text chunks, EXIF / XMP segments), not the pixels."""
    out = b''
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        pos = 8
        while pos + 8 <= len(data):
            length, kind = int.from_bytes(data[pos:pos + 4], 'big'), data[pos + 4:pos + 8]
            if kind in (b'tEXt', b'iTXt', b'zTXt', b'eXIf'):
                out += data[pos + 8:pos + 8 + length] + b'\n'
            pos += 12 + length
    elif data[:2] == b'\xff\xd8':
        pos = 2
        while pos + 4 <= len(data) and data[pos] == 0xff:
            marker, length = data[pos + 1], int.from_bytes(data[pos + 2:pos + 4], 'big')
            if marker in (0xe1, 0xed, 0xfe):
                out += data[pos + 4:pos + 2 + length] + b'\n'
            if marker == 0xda:
                break
            pos += 2 + length
    return out


def scan_member(where, data, findings, problems_contents):
    if data[:4] in BINARY_MAGIC or data[:2] == b'MZ':
        problems_contents.append('%s: native binary' % where)
        return
    lower = where.lower()
    if lower.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
        meta = image_metadata(data)
        for rule, rx in META_RULES:
            for m in rx.finditer(meta):
                findings.append((where, 0, rule, fingerprint(rule, m.group(0))))
        return
    if len(data) <= TEXT_LIMIT:
        scan_text(where, data, findings)


def norm_xml(text):
    return re.sub(r'\s+', ' ', re.sub(r'^\s*<\?xml[^>]*\?>', '', text)).strip()


def version_key(v):
    return [int(p) if p.isdigit() else p for p in re.split(r'[.+~\-]', v)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('site')
    ap.add_argument('--lock', default=os.path.join(ROOT, 'config', 'release.lock.json'))
    ap.add_argument('--allowlist', default=os.path.join(ROOT, 'config', 'scan-allowlist.json'))
    ap.add_argument('--private-patterns', help="the owner's private patterns file (JSON: {\"hard_patterns\": [regex, ...]})")
    ap.add_argument('--public-only', action='store_true', help='run without the private patterns (CI); not a publication clearance')
    ap.add_argument('--write-baseline', action='store_true', help='write the current findings to the allowlist (review them first)')
    ap.add_argument('--report', help='also write the output to this file')
    a = ap.parse_args()
    site, gate = os.path.abspath(a.site), Gate()
    zips = os.path.join(site, 'zips')
    lock = json.load(open(a.lock, encoding='utf-8'))
    locked = {e['id']: e for e in lock['addons']}
    hashes, index_p, packages, repo_p, lock_p, contents, findings = [], [], [], [], [], [], []

    # ---- index and hashes
    index_path = os.path.join(zips, 'addons.xml')
    if not os.path.isfile(index_path):
        gate.check('index', ['zips/addons.xml is missing'])
        return finish(gate, a)
    index = read(index_path)
    if first_word(index_path + '.sha256') != sha256(index):
        hashes.append('zips/addons.xml.sha256 does not match zips/addons.xml')
    if first_word(index_path + '.md5') != hashlib.md5(index).hexdigest():
        hashes.append('zips/addons.xml.md5 does not match zips/addons.xml (installs made before repository.chadflix 1.0.1 poll it)')
    entries = {}
    for m in re.finditer(r'<addon\b[^>]*\bid="([^"]+)"[^>]*>.*?</addon>', index.decode('utf-8', 'replace'), re.S):
        v = re.search(r'\bversion="([^"]+)"', m.group(0)[:m.group(0).index('>')])
        entries[m.group(1)] = (v.group(1) if v else '', m.group(0))
    if not entries:
        index_p.append('zips/addons.xml lists no add-on')
    manifest_path = os.path.join(site, 'release-manifest.json')
    manifest = json.load(open(manifest_path, encoding='utf-8')) if os.path.isfile(manifest_path) else None
    if manifest is None:
        hashes.append('release-manifest.json is missing')
    elif manifest['index']['sha256'] != sha256(index):
        hashes.append('release-manifest.json: the index hash does not match zips/addons.xml')
    in_manifest = {i['id']: i for i in (manifest or {}).get('addons', [])}
    expected_zips = set()
    for addon_id, (version, entry_xml) in sorted(entries.items()):
        rel = 'zips/%s/%s-%s.zip' % (addon_id, addon_id, version)
        expected_zips.add(rel)
        path = os.path.join(site, rel)
        if not os.path.isfile(path):
            index_p.append('%s: the index lists version %s but %s is missing' % (addon_id, version, rel))
            continue
        data = read(path)
        digest = sha256(data)
        if first_word(path + '.sha256') != digest:
            hashes.append('%s.sha256 does not match the zip' % rel)
        if addon_id in in_manifest and (in_manifest[addon_id]['sha256'] != digest or in_manifest[addon_id]['version'] != version):
            hashes.append('release-manifest.json does not match %s' % rel)
        if manifest is not None and addon_id not in in_manifest:
            hashes.append('release-manifest.json does not list %s' % addon_id)
        # ---- lock
        if addon_id not in locked:
            lock_p.append('%s is not in the lock file' % addon_id)
        else:
            if locked[addon_id]['version'] != version:
                lock_p.append('%s: version %s, the lock file says %s' % (addon_id, version, locked[addon_id]['version']))
            previous = lock.get('previous_versions', {}).get(addon_id)
            if previous and version_key(version) < version_key(previous):
                lock_p.append('%s: version %s is lower than the previous release (%s): Kodi would not update' % (addon_id, version, previous))
            src = locked[addon_id]['source']
            if src['type'] == 'carry' and src['sha256'] != digest:
                lock_p.append('%s: a carried (unmodified upstream) zip changed' % addon_id)
        # ---- package
        try:
            z = zipfile.ZipFile(path)
            bad = z.testzip()
            if bad:
                packages.append('%s: damaged entry' % rel)
        except zipfile.BadZipFile:
            packages.append('%s is not a readable zip' % rel)
            continue
        packed_xml = None
        for info in z.infolist():
            name = info.filename
            where = 'zip!%s' % name          # no version in it: an allowlist entry survives a version bump, not a changed value
            if name.startswith('/') or '..' in name.split('/') or '\\' in name or re.match(r'^[A-Za-z]:', name):
                packages.append('%s: unsafe path' % where)
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                packages.append('%s: symbolic link' % where)
                continue
            if not name.startswith(addon_id + '/'):
                packages.append('%s: outside the %s/ folder' % (where, addon_id))
                continue
            if name.endswith('/'):
                continue
            inner = name[len(addon_id) + 1:]
            if BAD_NAMES.search(inner) and (addon_id + '/' + inner) not in STOCK_DB:
                contents.append('%s: file type that must not ship' % where)
            if info.file_size > 64 * 1024 * 1024:
                contents.append('%s: larger than 64 MB' % where)
                continue
            member = z.read(info)
            if inner == 'addon.xml':
                packed_xml = member.decode('utf-8', 'replace')
            scan_member(where, member, findings, contents)
        if packed_xml is None:
            packages.append('%s: no addon.xml' % rel)
        elif norm_xml(packed_xml) != norm_xml(entry_xml):
            index_p.append('%s: the index entry differs from the addon.xml in the zip' % addon_id)
    for folder, _dirs, files in os.walk(zips):
        for f in files:
            rel = os.path.relpath(os.path.join(folder, f), site).replace(os.sep, '/')
            if f.endswith('.zip') and rel not in expected_zips:
                index_p.append('%s is not in the index (a stale or stray zip)' % rel)
            if f.endswith('.zip.sha256') and rel[:-7] not in expected_zips:
                index_p.append('%s has no zip in the index' % rel)
    for addon_id in locked:
        if addon_id not in entries:
            lock_p.append('%s is in the lock file but not in the index' % addon_id)

    # ---- repository add-on
    repo_id = lock['repository_addon']
    if repo_id in entries:
        repo_zip = '%s-%s.zip' % (repo_id, entries[repo_id][0])
        root_copy, zips_copy = os.path.join(site, repo_zip), os.path.join(zips, repo_id, repo_zip)
        if not os.path.isfile(root_copy) or not os.path.isfile(zips_copy) or read(root_copy) != read(zips_copy):
            repo_p.append('the two copies of %s differ or one is missing' % repo_zip)
        xml = entries[repo_id][1]
        if not re.search(r'<checksum\s+verify="sha256">[^<]+/zips/addons\.xml\.sha256</checksum>', xml):
            repo_p.append('the repository add-on does not verify the index with SHA-256')
        if not re.search(r'<hashes>sha256</hashes>', xml):
            repo_p.append('the repository add-on does not verify packages with SHA-256')
        if not re.search(r'<info[^>]*>https://[^<]+/zips/addons\.xml</info>', xml) and not manifest_is_local(manifest):
            repo_p.append('the repository add-on does not use an https address')
        if [f for f in os.listdir(site) if f.startswith(repo_id + '-') and f.endswith('.zip') and f != repo_zip]:
            repo_p.append('an older repository zip is still in the site root')
    else:
        repo_p.append('%s is not in the index' % repo_id)

    # ---- the tree outside the zips (page, lock, sources when SITE is the repository root)
    for folder, dirs, files in os.walk(site):
        dirs[:] = [d for d in dirs if d not in ('.git', 'zips')]
        for f in files:
            path = os.path.join(folder, f)
            rel = os.path.relpath(path, site).replace(os.sep, '/')
            if f.endswith('.zip') or os.path.getsize(path) > TEXT_LIMIT:
                continue
            scan_member(rel, read(path), findings, contents)

    # ---- secrets against the allowlist
    allow = {}
    if os.path.isfile(a.allowlist):
        for item in json.load(open(a.allowlist, encoding='utf-8'))['allow']:
            allow[(item['where'], item['rule'], item['fingerprint'])] = item.get('reason', '')
    unreviewed = sorted({f for f in findings if (f[0], f[2], f[3]) not in allow})
    if a.write_baseline:
        items = sorted({(w, r, fp) for w, _n, r, fp in findings})
        json.dump({'_comment': 'Reviewed findings of tools/validate_release.py: one entry per place, rule and value fingerprint. '
                               'Never a whole file, never a whole rule. A new or changed value is a new finding.',
                   'allow': [{'where': w, 'rule': r, 'fingerprint': fp, 'reason': allow.get((w, r, fp), 'REVIEW ME')} for w, r, fp in items]},
                  open(a.allowlist, 'w', encoding='utf-8'), indent=1)
        gate.say('baseline written: %d entries (%d new, marked REVIEW ME)' % (len(items), len([i for i in items if i not in allow])))
        unreviewed = []
    secrets = ['%s:%d  %s  fingerprint %s' % f for f in unreviewed]
    unreasoned = [k for k, reason in allow.items() if not reason or reason == 'REVIEW ME']
    if unreasoned and not a.write_baseline:
        secrets.append('%d allowlist entries have no reviewed reason (REVIEW ME)' % len(unreasoned))

    gate.check('hashes', hashes)
    gate.check('index', index_p)
    gate.check('packages', packages)
    gate.check('repository', repo_p)
    gate.check('lock', lock_p)
    gate.check('contents', contents)
    gate.check('secrets', secrets)

    # ---- private patterns
    if a.private_patterns:
        problems = []
        try:
            private = json.load(open(a.private_patterns, encoding='utf-8'))
            hard = [re.compile(p) for p in private['hard_patterns']]
            # "add-on/path:line" places the owner reviewed (upstream text that only looks private)
            reviewed = set(private.get('hard_allow', []))
        except Exception:
            hard = None
        if not hard:
            gate.check('private', [], not_run='the private patterns file could not be read or is empty')
        else:
            def private_scan(where, data, key=None):
                try:
                    text = data.decode('utf-8')
                except UnicodeDecodeError:
                    text = image_metadata(data).decode('latin-1')
                for number, line in enumerate(text.splitlines(), 1):
                    for i, rx in enumerate(hard):
                        if rx.search(line) and '%s:%d' % (key or where, number) not in reviewed:
                            problems.append('%s:%d  private pattern #%d' % (where, number, i + 1))   # never the pattern or the text
            for rel in sorted(expected_zips):
                path = os.path.join(site, rel)
                if os.path.isfile(path):
                    with zipfile.ZipFile(path) as z:
                        for info in z.infolist():
                            if not info.filename.endswith('/') and info.file_size <= TEXT_LIMIT:
                                private_scan('%s!%s' % (rel, info.filename), z.read(info), info.filename)
            for folder, dirs, files in os.walk(site):
                dirs[:] = [d for d in dirs if d not in ('.git', 'zips')]
                for f in files:
                    path = os.path.join(folder, f)
                    if not f.endswith('.zip') and os.path.getsize(path) <= TEXT_LIMIT and os.path.abspath(path) != os.path.abspath(a.private_patterns):
                        private_scan(os.path.relpath(path, site), read(path))
            gate.check('private', problems)
    elif a.public_only:
        gate.say('CHECK private    NOT RUN (--public-only): this run does not clear the tree for publication')
    else:
        gate.check('private', [], not_run='no --private-patterns file was given (use --public-only for a CI run)')
    return finish(gate, a)


def manifest_is_local(manifest):
    return bool(manifest) and manifest.get('base_url', '').startswith('http://127.0.0.1')


def finish(gate, a):
    ok = not gate.failed
    gate.say('RESULT: %s' % ('all checks passed' if ok else 'FAILED: ' + ', '.join(sorted(gate.failed))))
    if a.report:
        with open(a.report, 'w', encoding='utf-8') as f:
            f.write('\n'.join(gate.lines) + '\n')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
