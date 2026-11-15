#!/usr/bin/env python3
"""Build the ChadFlix repository release from clean, declared inputs only (security audit F-06, F-10).

    python3 tools/build_release.py --out /some/empty/dir --upstream-dir /dir/with/upstream/zips
    python3 tools/build_release.py --out ... --upstream-dir ... --base-url http://127.0.0.1:8765/   (a local test site)

Everything it reads is named in config/release.lock.json and is either tracked in this repository or a public upstream
archive pinned by SHA-256. It never reads a device pull, a Kodi install, a home directory or a private settings file.
A missing or changed input is an error, never a partial build.

Kinds of add-on source in the lock file:
  tree             an add-on written here (src/<id>/): Setup and the repository add-on. "{...}" fields in addon.xml.in and
                   setup.template.json are filled from the lock file.
  upstream+overlay a public upstream archive (URL and SHA-256 pinned) plus the tracked ChadFlix changes in
                   inputs/overlays/<id>/ (whole files: changed and added) and inputs/overlays/<id>.removed.txt.
  carry            an unmodified upstream add-on: the zip already published here is carried over byte for byte
                   (SHA-256 pinned). Its upstream URL is recorded so anyone can compare the contents (tools/verify_upstream.py).

Output (the Pages tree's generated part):
  index.html, repository.chadflix-<v>.zip, zips/addons.xml, zips/addons.xml.md5 (kept for installs made before
  repository.chadflix 1.0.1), zips/addons.xml.sha256, zips/<id>/<id>-<v>.zip, zips/<id>/<id>-<v>.zip.sha256, artwork,
  and release-manifest.json (every zip's SHA-256 tied to its inputs).

Zips are deterministic: sorted entries, fixed timestamps and modes, zlib level 9. Two builds from the same inputs with the
same Python/zlib give identical bytes; the manifest records both versions so a mismatch can be explained.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = (2026, 11, 12, 0, 0, 0)       # the fixed entry time used since release 1
SKIP_DIRS = {'__pycache__', '.git', '.svn'}
SKIP_FILES = {'.DS_Store', 'Thumbs.db'}
INDEX_TEMPLATE_ZIP = 'repository.chadflix-1.0.0.zip'   # the name written in tools/index.html.in


def die(msg):
    sys.exit('BUILD FAILED: %s' % msg)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def tracked(rel):
    """An input path inside this repository; refuses anything that points outside it."""
    path = os.path.normpath(os.path.join(ROOT, rel))
    if os.path.isabs(rel) or not (path + os.sep).startswith(ROOT + os.sep):
        die('input outside the repository: %s' % rel)
    if not os.path.exists(path):
        die('missing input: %s' % rel)
    return path


def walk(root):
    out = []
    for r, ds, fs in os.walk(root):
        ds[:] = sorted(d for d in ds if d not in SKIP_DIRS)
        for f in sorted(fs):
            if f in SKIP_FILES or f.endswith(('.pyc', '.pyo')):
                continue
            out.append(os.path.relpath(os.path.join(r, f), root).replace(os.sep, '/'))
    return sorted(out)


def read_tree(root):
    files = {}
    for rel in walk(root):
        if os.path.islink(os.path.join(root, rel)):
            die('symbolic link in an input tree: %s' % os.path.join(root, rel))
        with open(os.path.join(root, rel), 'rb') as f:
            files[rel] = f.read()
    return files


def tree_digest(files):
    """One SHA-256 over sorted (path, content hash) pairs: the identity of an add-on's contents, whatever the zip."""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode('utf-8') + b'\0' + hashlib.sha256(files[rel]).digest())
    return h.hexdigest()


def read_zip(path, addon_id, root_name=None):
    """{relative path: bytes} of an add-on zip whose single top folder is root_name (default: the add-on id)."""
    root_name = root_name or addon_id
    files = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename
            if name.startswith('/') or '..' in name.split('/') or '\\' in name:
                die('unsafe path in %s: %s' % (os.path.basename(path), name))
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                die('symbolic link in %s: %s' % (os.path.basename(path), name))
            if name.endswith('/'):
                continue
            top, _, rel = name.partition('/')
            if top != root_name or not rel:
                die('%s: entry outside the %s/ folder: %s' % (os.path.basename(path), root_name, name))
            files[rel] = z.read(info)
    return files


def zip_bytes(addon_id, files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        dirs = set()
        for rel in sorted(files):
            parts = rel.split('/')[:-1]
            for i in range(len(parts) + 1):
                dname = '/'.join([addon_id] + parts[:i]) + '/'
                if dname not in dirs:
                    dirs.add(dname)
                    zi = zipfile.ZipInfo(dname, date_time=STAMP)
                    zi.external_attr = (0o40755 << 16) | 0x10
                    z.writestr(zi, b'')
            zi = zipfile.ZipInfo(addon_id + '/' + rel, date_time=STAMP)
            zi.external_attr = 0o644 << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, files[rel], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue()


def addon_version(xml_text, addon_id):
    m = re.search(r'<addon\b[^>]*>', xml_text, re.S)
    tag = m.group(0) if m else ''
    i = re.search(r'\bid="([^"]+)"', tag)
    v = re.search(r'\bversion="([^"]+)"', tag)
    if not i or not v or i.group(1) != addon_id:
        die('%s: addon.xml does not name this add-on' % addon_id)
    return v.group(1)


def asset_paths(xml_text):
    m = re.search(r'<assets>(.*?)</assets>', xml_text, re.S)
    return [p.strip() for p in re.findall(r'<(?:icon|fanart)>([^<]+)</', m.group(1))] if m else []


def upstream_archive(spec, upstream_dir):
    if not upstream_dir:
        die('--upstream-dir is required: %s must be there (from %s)' % (spec['file'], spec['url']))
    path = os.path.join(upstream_dir, spec['file'])
    if not os.path.isfile(path):
        die('missing upstream archive %s (download it from %s into --upstream-dir)' % (spec['file'], spec['url']))
    if sha256_file(path) != spec['sha256']:
        die('upstream archive %s does not have the pinned SHA-256' % spec['file'])
    return path


def build_addon(entry, lock, base_url, upstream_dir, versions):
    addon_id, src = entry['id'], entry['source']
    kind = src['type']
    info = {'id': addon_id, 'version': entry['version'], 'source': kind}
    if kind == 'carry':
        path = tracked(src['zip'])
        if sha256_file(path) != src['sha256']:
            die('%s: the carried zip does not have the pinned SHA-256' % addon_id)
        files = read_zip(path, addon_id)
        with open(path, 'rb') as f:
            data = f.read()
        info.update(carried_from=src['zip'], upstream=src.get('upstream'))
    elif kind == 'upstream+overlay':
        up = src['upstream']
        files = read_zip(upstream_archive(up, upstream_dir), addon_id, up.get('root'))
        overlay = read_tree(tracked(src['overlay']))
        removed = [l.strip() for l in open(tracked(src['removed']), encoding='utf-8') if l.strip() and not l.startswith('#')]
        for rel in removed:
            if rel not in files:
                die('%s: %s is listed as removed but is not in the upstream archive' % (addon_id, rel))
            if rel in overlay:
                die('%s: %s is both removed and in the overlay' % (addon_id, rel))
            del files[rel]
        changed = sorted(r for r in overlay if r in files)
        added = sorted(r for r in overlay if r not in files)
        unchanged = [r for r in changed if files[r] == overlay[r]]
        if unchanged:
            die('%s: overlay files identical to upstream (remove them from the overlay): %s' % (addon_id, unchanged[:5]))
        files.update(overlay)
        data = zip_bytes(addon_id, files)
        info.update(upstream={k: up[k] for k in ('url', 'file', 'sha256')}, overlay=src['overlay'], overlay_sha256=tree_digest(overlay),
                    changed=len(changed), added=len(added), removed=len(removed))
    elif kind == 'tree':
        files = read_tree(tracked(src['path']))
        template = files.pop('addon.xml.in', None)
        if template is None:
            die('%s: no addon.xml.in in %s' % (addon_id, src['path']))
        text = template.decode('utf-8')
        fields = {'{ID}': addon_id, '{NAME}': entry['name'], '{VERSION}': entry['version'], '{BASE_URL}': base_url,
                  '{SOURCE_URL}': 'https://github.com/%s/%s' % (lock['github_user'], lock['repo_slug'])}
        if 'requires' in entry:
            reqs = [[a, versions.get(a) or lock['official_versions'].get(a, '')] for a in entry['requires']]
            fields['{REQUIRES}'] = '\n'.join('        <import addon="%s" version="%s" />' % (a, v) if v else
                                             '        <import addon="%s" />' % a for a, v in reqs)
            setup = json.loads(files.pop('resources/setup.template.json').decode('utf-8'))
            setup['requires'] = reqs
            files['resources/setup.json'] = json.dumps(setup, indent=1).encode('utf-8')
        for key, value in fields.items():
            text = text.replace(key, value)
        if re.search(r'\{[A-Z_]+\}', text):
            die('%s: unfilled field in addon.xml.in' % addon_id)
        files['addon.xml'] = text.encode('utf-8')
        data = zip_bytes(addon_id, files)
        info.update(tree=src['path'], tree_sha256=tree_digest(files))
    else:
        die('%s: unknown source type %s' % (addon_id, kind))
    xml_text = files['addon.xml'].decode('utf-8')
    if addon_version(xml_text, addon_id) != entry['version']:
        die('%s: addon.xml says version %s, the lock file says %s' % (addon_id, addon_version(xml_text, addon_id), entry['version']))
    info.update(zip='zips/%s/%s-%s.zip' % (addon_id, addon_id, entry['version']), sha256=sha256_bytes(data), size=len(data),
                files=len(files), content_sha256=tree_digest(files))
    return files, data, xml_text, info


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', required=True, help='an empty or new directory for the generated tree')
    ap.add_argument('--upstream-dir', help='directory holding the pinned upstream archives')
    ap.add_argument('--lock', default='config/release.lock.json')
    ap.add_argument('--base-url', help='repository URL for a local test (default: the GitHub Pages address)')
    args = ap.parse_args()
    lock_path = tracked(args.lock)
    lock = json.load(open(lock_path, encoding='utf-8'))
    out = os.path.abspath(args.out)
    if os.path.exists(out) and os.listdir(out):
        die('--out must be empty: %s' % out)
    base_url = args.base_url or 'https://%s.github.io/%s/' % (lock['github_user'].lower(), lock['repo_slug'])
    if not base_url.endswith('/'):
        base_url += '/'
    zips = os.path.join(out, 'zips')
    os.makedirs(zips)
    versions = {e['id']: e['version'] for e in lock['addons']}
    ids = [e['id'] for e in lock['addons']]
    if len(set(ids)) != len(ids):
        die('an add-on is listed twice in the lock file')
    parts, manifest = [], []
    for entry in sorted(lock['addons'], key=lambda e: e['id']):
        files, data, xml_text, info = build_addon(entry, lock, base_url, args.upstream_dir, versions)
        if len(data) > 95 * 1024 * 1024:
            die('%s is over 95 MB (GitHub limit is 100 MB per file)' % info['zip'])
        folder = os.path.join(zips, entry['id'])
        os.makedirs(folder)
        name = '%s-%s.zip' % (entry['id'], entry['version'])
        with open(os.path.join(folder, name), 'wb') as f:
            f.write(data)
        with open(os.path.join(folder, name + '.sha256'), 'w') as f:      # Kodi reads up to the first space or newline
            f.write(info['sha256'] + '\n')
        for asset in asset_paths(xml_text):
            if asset in files:
                dst = os.path.join(folder, asset)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, 'wb') as f:
                    f.write(files[asset])
        parts.append(re.sub(r'^\s*<\?xml[^>]*\?>\s*', '', xml_text).strip())
        manifest.append(info)
    index = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n' + '\n'.join(parts) + '\n</addons>\n').encode('utf-8')
    with open(os.path.join(zips, 'addons.xml'), 'wb') as f:
        f.write(index)
    # .md5: the change marker repository.chadflix 1.0.0 polls (it never verified it); kept so those installs find 1.0.1.
    with open(os.path.join(zips, 'addons.xml.md5'), 'w') as f:
        f.write(hashlib.md5(index).hexdigest())
    with open(os.path.join(zips, 'addons.xml.sha256'), 'w') as f:
        f.write(sha256_bytes(index) + '\n')
    repo = next(e for e in lock['addons'] if e['id'] == lock['repository_addon'])
    repo_zip = '%s-%s.zip' % (repo['id'], repo['version'])
    shutil.copyfile(os.path.join(zips, repo['id'], repo_zip), os.path.join(out, repo_zip))
    html = open(tracked('tools/index.html.in'), encoding='utf-8').read().replace(INDEX_TEMPLATE_ZIP, repo_zip)
    link = '<a href="%s">%s</a>' % (repo_zip, repo_zip)
    if html.count(link) != 1:
        die('tools/index.html.in must contain exactly one %s (Kodi lists only that form)' % link)
    with open(os.path.join(out, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    release = {'release': lock['release'], 'base_url': base_url, 'lock_sha256': sha256_file(lock_path),
               'index': {'file': 'zips/addons.xml', 'sha256': sha256_bytes(index), 'md5': hashlib.md5(index).hexdigest()},
               'repository_zip': repo_zip, 'addons': manifest,
               'built_with': {'python': '%d.%d.%d' % sys.version_info[:3], 'zlib': zlib.ZLIB_RUNTIME_VERSION,
                              'builder_sha256': sha256_file(os.path.abspath(__file__))}}
    with open(os.path.join(out, 'release-manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(release, f, indent=1, sort_keys=True)
        f.write('\n')
    total = sum(i['size'] for i in manifest)
    for i in manifest:
        print('  %-42s %-14s %7.1f MB  %s  (%s)' % (i['id'], i['version'], i['size'] / 1048576.0, i['sha256'][:16], i['source']))
    print('built %d add-ons, %.1f MB, index sha256 %s' % (len(manifest), total / 1048576.0, release['index']['sha256']))


if __name__ == '__main__':
    main()
