#!/usr/bin/env python3
"""Check that every unmodified add-on carried in this repository has exactly its upstream author's contents (security audit F-09).

    python3 tools/verify_upstream.py            fetch each upstream archive named in config/release.lock.json (into memory only)
    python3 tools/verify_upstream.py --dir D    use archives already downloaded into D (named as in the URL)

For each "carry" add-on the upstream archive is compared with the carried zip file by file (path and SHA-256 of the
contents, ignoring the zip container and the name of the top folder). "identical" is only printed after that comparison.
This is the one tool here that uses the network, and only for the public upstream URLs in the lock file.
"""
import argparse
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def contents(data):
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if not info.filename.endswith('/'):
                out[info.filename.partition('/')[2]] = hashlib.sha256(z.read(info)).hexdigest()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir')
    ap.add_argument('--lock', default=os.path.join(ROOT, 'config', 'release.lock.json'))
    a = ap.parse_args()
    bad = 0
    for entry in json.load(open(a.lock, encoding='utf-8'))['addons']:
        src = entry['source']
        if src['type'] != 'carry':
            continue
        url = src['upstream']['url']
        try:
            if a.dir:
                with open(os.path.join(a.dir, url.rsplit('/', 1)[1]), 'rb') as f:
                    upstream = f.read()
            else:
                if not url.startswith('https://'):
                    raise ValueError('not an https URL')
                with urllib.request.urlopen(url, timeout=60) as r:
                    upstream = r.read()
            with open(os.path.join(ROOT, src['zip']), 'rb') as f:
                ours = f.read()
            if hashlib.sha256(ours).hexdigest() != src['sha256']:
                raise ValueError('the carried zip does not have the pinned SHA-256')
            a_files, b_files = contents(upstream), contents(ours)
            if a_files == b_files:
                print('identical   %-42s %-8s %5d files   upstream archive sha256 %s' % (entry['id'], entry['version'], len(b_files), hashlib.sha256(upstream).hexdigest()))
            else:
                bad += 1
                print('DIFFERENT   %-42s %d only upstream, %d only here, %d changed' % (
                    entry['id'], len(set(a_files) - set(b_files)), len(set(b_files) - set(a_files)),
                    sum(1 for k in a_files if k in b_files and a_files[k] != b_files[k])))
        except Exception as e:
            bad += 1
            print('NOT CHECKED %-42s %s: %s' % (entry['id'], type(e).__name__, str(e)[:80]))
    print('RESULT: %s' % ('every carried add-on equals its upstream archive' if not bad else '%d add-on(s) not confirmed' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
