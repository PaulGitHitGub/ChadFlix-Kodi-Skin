#!/usr/bin/env python3
"""Compare two release trees: python3 tools/compare_release.py A B

Equal means: the same index bytes, the same add-ons and versions, and for every add-on zip the same files with the same
contents. Zip bytes are compared too and reported, but a difference there alone is not a failure: another zlib version
packs the same files into different bytes (release-manifest.json records the Python and zlib that built a tree).
"""
import hashlib
import os
import sys
import zipfile


def tree(root):
    zips, index = {}, open(os.path.join(root, 'zips', 'addons.xml'), 'rb').read()
    for folder, _d, files in os.walk(os.path.join(root, 'zips')):
        for f in files:
            if f.endswith('.zip'):
                path = os.path.join(folder, f)
                with zipfile.ZipFile(path) as z:
                    content = sorted((i.filename, hashlib.sha256(z.read(i)).hexdigest()) for i in z.infolist() if not i.filename.endswith('/'))
                zips[f] = (hashlib.sha256(repr(content).encode()).hexdigest(), hashlib.sha256(open(path, 'rb').read()).hexdigest())
    return index, zips


def main():
    (ia, za), (ib, zb) = tree(sys.argv[1]), tree(sys.argv[2])
    bad = []
    if ia != ib:
        bad.append('zips/addons.xml differs')
    for name in sorted(set(za) | set(zb)):
        if name not in za or name not in zb:
            bad.append('%s is only in one tree' % name)
        elif za[name][0] != zb[name][0]:
            bad.append('%s: different contents' % name)
        elif za[name][1] != zb[name][1]:
            print('note: %s has the same contents but different zip bytes (another zlib?)' % name)
    for line in bad:
        print('DIFFERENT: ' + line)
    print('RESULT: %s (%d zips)' % ('same release' if not bad else 'NOT the same release', len(za)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
