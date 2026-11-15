"""tools/validate_release.py must fail on a bad release and must never print what it found.
Synthetic fixtures only. Run: python3 -B -m unittest discover -s tools/tests -v"""
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True
sys.path.insert(0, TOOLS)
import build_release as br

MARK = 'SYNTHmarker'            # every planted value contains this; no output may ever contain it
BASE = 'https://example.github.io/Repo/'
REPO_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="repository.test" name="Test" version="1.0.1" provider-name="t">
    <extension point="xbmc.addon.repository" name="Test">
        <dir>
            <info compressed="false">%szips/addons.xml</info>
            <checksum verify="sha256">%szips/addons.xml.sha256</checksum>
            <datadir zip="true">%szips/</datadir>
            <hashes>sha256</hashes>
        </dir>
    </extension>
</addon>''' % (BASE, BASE, BASE)
ADDON_XML = '<?xml version="1.0" encoding="UTF-8"?>\n<addon id="script.test" name="T" version="2.0.0" provider-name="t">\n</addon>'


def png(text_chunk=None):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    out = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
    if text_chunk:
        out += chunk(b'tEXt', text_chunk)
    return out + chunk(b'IDAT', zlib.compress(b'\x00\x00\x00\x00')) + chunk(b'IEND', b'')


class Site:
    def __init__(self, extra_files=None, raw_entries=None, versions=None):
        self.dir = tempfile.mkdtemp(prefix='validate-test')
        self.site = os.path.join(self.dir, 'site')
        versions = versions or {'repository.test': '1.0.1', 'script.test': '2.0.0'}
        addons = {'repository.test': {'addon.xml': REPO_XML.replace('version="1.0.1"', 'version="%s"' % versions['repository.test']).encode()},
                  'script.test': dict({'addon.xml': ADDON_XML.replace('2.0.0', versions['script.test']).encode(), 'default.py': b'print("hello")\n',
                                       'icon.png': png()}, **(extra_files or {}))}
        parts = []
        os.makedirs(os.path.join(self.site, 'zips'))
        for addon_id, files in sorted(addons.items()):
            data = br.zip_bytes(addon_id, files)
            if raw_entries and addon_id == 'script.test':
                buf = io.BytesIO(data)
                with zipfile.ZipFile(buf, 'a') as z:
                    for name, content, mode in raw_entries:
                        info = zipfile.ZipInfo(name, date_time=br.STAMP)
                        info.external_attr = mode << 16
                        z.writestr(info, content)
                data = buf.getvalue()
            folder = os.path.join(self.site, 'zips', addon_id)
            os.makedirs(folder)
            name = '%s-%s.zip' % (addon_id, versions[addon_id])
            open(os.path.join(folder, name), 'wb').write(data)
            open(os.path.join(folder, name + '.sha256'), 'w').write(hashlib.sha256(data).hexdigest() + '\n')
            parts.append(files['addon.xml'].decode().split('?>', 1)[-1].strip())
        self.write_index(('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n' + '\n'.join(parts) + '\n</addons>\n').encode())
        repo_zip = 'repository.test-%s.zip' % versions['repository.test']
        open(os.path.join(self.site, repo_zip), 'wb').write(open(os.path.join(self.site, 'zips', 'repository.test', repo_zip), 'rb').read())
        self.lock = os.path.join(self.dir, 'lock.json')
        json.dump({'repository_addon': 'repository.test', 'previous_versions': {'script.test': '2.0.0', 'repository.test': '1.0.0'},
                   'addons': [{'id': a, 'version': versions[a], 'source': {'type': 'tree'}} for a in addons]}, open(self.lock, 'w'))
        self.allow = os.path.join(self.dir, 'allow.json')
        json.dump({'allow': []}, open(self.allow, 'w'))
        self.write_manifest()

    def write_index(self, index):
        z = os.path.join(self.site, 'zips')
        open(os.path.join(z, 'addons.xml'), 'wb').write(index)
        open(os.path.join(z, 'addons.xml.sha256'), 'w').write(hashlib.sha256(index).hexdigest() + '\n')
        open(os.path.join(z, 'addons.xml.md5'), 'w').write(hashlib.md5(index).hexdigest())

    def write_manifest(self):
        z = os.path.join(self.site, 'zips')
        addons = []
        for addon_id in sorted(os.listdir(z)):
            if os.path.isdir(os.path.join(z, addon_id)):
                name = [f for f in os.listdir(os.path.join(z, addon_id)) if f.endswith('.zip')][0]
                addons.append({'id': addon_id, 'version': name[len(addon_id) + 1:-4],
                               'sha256': hashlib.sha256(open(os.path.join(z, addon_id, name), 'rb').read()).hexdigest()})
        index = open(os.path.join(z, 'addons.xml'), 'rb').read()
        json.dump({'base_url': BASE, 'index': {'sha256': hashlib.sha256(index).hexdigest()}, 'addons': addons},
                  open(os.path.join(self.site, 'release-manifest.json'), 'w'))

    def run(self, *args):
        cmd = [sys.executable, '-B', os.path.join(TOOLS, 'validate_release.py'), self.site, '--lock', self.lock, '--allowlist', self.allow] + list(args)
        p = subprocess.run(cmd, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr


class Validate(unittest.TestCase):
    def expect_fail(self, site, check, *args):
        code, out = site.run(*(args or ('--public-only',)))
        self.assertNotEqual(code, 0, out)
        self.assertRegex(out, r'CHECK %s\s+(FAILED|NOT RUN)' % check, out)
        self.assertNotIn(MARK, out)                       # the report never shows what it found
        return out

    def test_good_release_passes(self):
        code, out = Site().run('--public-only')
        self.assertEqual(code, 0, out)
        self.assertIn('RESULT: all checks passed', out)
        self.assertIn('private    NOT RUN', out)           # and says it is not a publication clearance

    def test_changed_zip_after_hashing(self):
        s = Site()
        path = os.path.join(s.site, 'zips', 'script.test', 'script.test-2.0.0.zip')
        open(path, 'ab').write(b'tail')
        self.expect_fail(s, 'hashes')

    def test_changed_index_after_hashing(self):
        s = Site()
        path = os.path.join(s.site, 'zips', 'addons.xml')
        open(path, 'ab').write(b'\n')
        self.expect_fail(s, 'hashes')

    def test_index_entry_differs_from_the_packed_manifest(self):
        s = Site()
        index = open(os.path.join(s.site, 'zips', 'addons.xml'), 'rb').read().replace(b'name="T"', b'name="Other"')
        s.write_index(index)
        s.write_manifest()
        self.expect_fail(s, 'index')

    def test_stray_zip(self):
        s = Site()
        open(os.path.join(s.site, 'zips', 'script.test', 'script.test-1.9.0.zip'), 'wb').write(b'PK')
        self.expect_fail(s, 'index')

    def test_traversal_and_symlink_entries(self):
        self.expect_fail(Site(raw_entries=[('script.test/../../evil.py', b'x', 0o100644)]), 'packages')
        self.expect_fail(Site(raw_entries=[('script.test/link', b'/etc/passwd', 0o120777)]), 'packages')
        self.expect_fail(Site(raw_entries=[('other.addon/file.py', b'x', 0o100644)]), 'packages')

    def test_files_that_must_not_ship(self):
        for name, data in (('lib/x.pyc', b'\x00'), ('userdata.db', b'SQLite format 3\x00'), ('kodi.log', b'log'), ('__pycache__/a.txt', b'x'),
                           ('bin/tool', b'\x7fELF\x02\x01\x01'), ('nested.zip', b'PK\x03\x04')):
            self.expect_fail(Site(extra_files={name: data}), 'contents')

    def test_secret_shapes_fail_without_being_printed(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJ%sIjoiMSJ9.sgn%ssgn012345' % (MARK, MARK)
        cases = {
            'xml element': ('resources/a.xml', '<settings><token>%s0123456789abcdef</token></settings>' % MARK),
            'xml attribute': ('resources/b.xml', '<item api_key="%s0123456789abcdef" />' % MARK),
            'xml setting default': ('resources/settings.xml', '<setting id="rd.token" default="%s0123456789abcdef" />' % MARK),
            'json': ('resources/c.json', '{"access_token": "%s0123456789abcdef", "n": 1}' % MARK),
            'python assignment': ('lib/d.py', 'API_KEY = "%s0123456789abcdef"\n' % MARK),
            'bearer url': ('lib/e.py', 'u = "https://api.example/x?token=%s0123456789abcdef&a=1"\n' % MARK),
            'bearer header': ('lib/f.py', 'h = {"Authorization": "Bearer %s0123456789abcdef"}\n' % MARK),
            'dotted token': ('lib/g.txt', 'value %s\n' % jwt),
            'url password': ('lib/h.txt', 'ftp://someone:%sPassw0rd@host.example/x\n' % MARK),
            'private key': ('lib/i.pem.txt', '-----BEGIN RSA PRIVATE KEY-----\n%s\n' % MARK),
            'home path': ('lib/j.py', 'p = "/Users/%sname/Library/x"\n' % MARK),
            'lan address': ('lib/k.py', 'host = "192.168.77.%d"  # %s\n' % (42, 'x')),
        }
        for label, (name, text) in cases.items():
            out = self.expect_fail(Site(extra_files={name: text.encode()}), 'secrets')
            self.assertIn('fingerprint', out, label)
            self.assertNotIn('192.168.77.42', out)

    def test_image_metadata(self):
        for chunk in (b'Comment\x00made on /Users/%sname/Desktop/' % MARK.encode(), b'Author\x00%s@example.com' % MARK.encode(),
                      b'XML:com.adobe.xmp\x00<exif:GPSLatitude>47,36.1N</exif:GPSLatitude>'):
            self.expect_fail(Site(extra_files={'fanart.png': png(chunk)}), 'secrets')

    def test_reviewed_exception_is_one_value_in_one_place(self):
        text = 'APP_KEY_TOKEN = "%s0123456789abcdef"\n' % MARK
        s = Site(extra_files={'lib/keys.py': text.encode()})
        self.expect_fail(s, 'secrets')
        code, out = s.run('--public-only', '--write-baseline')
        allow = json.load(open(s.allow))
        self.assertEqual(len(allow['allow']), 1)
        self.expect_fail(s, 'secrets')                    # an unreviewed entry ("REVIEW ME") still blocks
        allow['allow'][0]['reason'] = 'upstream application identifier, reviewed'
        json.dump(allow, open(s.allow, 'w'))
        self.assertEqual(s.run('--public-only')[0], 0)
        # the same file with another value, or the same value in another file, is a new finding
        s2 = Site(extra_files={'lib/keys.py': text.replace('0123', '9999').encode()})
        json.dump(allow, open(s2.allow, 'w'))
        self.expect_fail(s2, 'secrets')
        s3 = Site(extra_files={'lib/other.py': text.encode()})
        json.dump(allow, open(s3.allow, 'w'))
        self.expect_fail(s3, 'secrets')

    def test_private_patterns_are_required_and_never_echoed(self):
        s = Site(extra_files={'lib/note.txt': ('written by %sOwnerName\n' % MARK).encode()})
        code, out = s.run()                                                      # neither --private-patterns nor --public-only
        self.assertNotEqual(code, 0)
        self.assertRegex(out, r'CHECK private\s+NOT RUN')
        self.assertNotIn(MARK, out)
        self.expect_fail(s, 'private', '--private-patterns', os.path.join(s.dir, 'missing.json'))
        patterns = os.path.join(s.dir, 'private.json')
        json.dump({'hard_patterns': ['%sOwnerName' % MARK]}, open(patterns, 'w'))
        out = self.expect_fail(s, 'private', '--private-patterns', patterns)
        self.assertIn('private pattern #1', out)
        clean = Site()
        self.assertEqual(clean.run('--private-patterns', patterns)[0], 0)

    def test_version_rules(self):
        self.expect_fail(Site(versions={'repository.test': '1.0.1', 'script.test': '1.9.9'}), 'lock')     # lower than the previous release
        s = Site()
        lock = json.load(open(s.lock))
        lock['addons'][1]['version'] = '2.0.1'
        json.dump(lock, open(s.lock, 'w'))
        self.expect_fail(s, 'lock')                                              # the lock file and the zip disagree

    def test_repository_addon_must_verify_sha256(self):
        s = Site()
        for old, new in (('<hashes>sha256</hashes>', ''), (' verify="sha256"', '')):
            t = Site()
            folder = os.path.join(t.site, 'zips', 'repository.test')
            data = br.zip_bytes('repository.test', {'addon.xml': REPO_XML.replace(old, new).encode()})
            for path in (os.path.join(folder, 'repository.test-1.0.1.zip'), os.path.join(t.site, 'repository.test-1.0.1.zip')):
                open(path, 'wb').write(data)
            open(os.path.join(folder, 'repository.test-1.0.1.zip.sha256'), 'w').write(hashlib.sha256(data).hexdigest())
            index = open(os.path.join(t.site, 'zips', 'addons.xml'), 'rb').read().replace(old.encode(), new.encode())
            t.write_index(index)
            t.write_manifest()
            self.expect_fail(t, 'repository')
        os.remove(os.path.join(s.site, 'repository.test-1.0.1.zip'))
        self.expect_fail(s, 'repository')


if __name__ == '__main__':
    unittest.main()
