#!/usr/bin/env python3
"""Maintainer step: turn an assembled add-on into the tracked inputs tools/build_release.py builds from (security audit F-06).

    python3 tools/derive_inputs.py ADDON_ID BUILT_ZIP UPSTREAM_ZIP [--upstream-root NAME]

Writes inputs/overlays/ADDON_ID/ (every file that differs from, or is not in, the upstream archive, as a whole file)
and inputs/overlays/ADDON_ID.removed.txt (upstream files the ChadFlix build drops). Then it rebuilds the add-on from
upstream + overlay in memory and stops unless the result equals BUILT_ZIP file for file: the overlay is the complete
ChadFlix change, nothing is lost and nothing extra is carried.

BUILT_ZIP is the add-on as tools/build_repo.py assembled it (the private stage that still reads the test device's
snapshot). After this step the public build no longer needs any of those private inputs.
"""
import argparse
import os
import shutil
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_release as br


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('addon_id')
    ap.add_argument('built_zip')
    ap.add_argument('upstream_zip')
    ap.add_argument('--upstream-root')
    ap.add_argument('--dest', default=os.path.join(br.ROOT, 'inputs', 'overlays'))
    a = ap.parse_args()
    built = br.read_zip(a.built_zip, a.addon_id)
    upstream = br.read_zip(a.upstream_zip, a.addon_id, a.upstream_root)
    overlay = {rel: data for rel, data in built.items() if upstream.get(rel) != data}
    removed = sorted(rel for rel in upstream if rel not in built)
    dest = os.path.join(a.dest, a.addon_id)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    for rel, data in sorted(overlay.items()):
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
    with open(dest + '.removed.txt', 'w', encoding='utf-8') as f:
        f.write('# upstream files that are not in the ChadFlix build of %s (one path per line)\n' % a.addon_id)
        f.write(''.join(rel + '\n' for rel in removed))
    rebuilt = dict(upstream)
    for rel in removed:
        del rebuilt[rel]
    rebuilt.update(br.read_tree(dest))
    if rebuilt != built:
        sys.exit('DERIVE FAILED: upstream + overlay does not equal the built add-on')
    changed = sum(1 for rel in overlay if rel in upstream)
    print('%s: %d changed, %d added, %d removed, %d unchanged upstream files; content sha256 %s; upstream archive sha256 %s'
          % (a.addon_id, changed, len(overlay) - changed, len(removed), len(built) - len(overlay), br.tree_digest(built),
             br.sha256_file(a.upstream_zip)))


if __name__ == '__main__':
    main()
