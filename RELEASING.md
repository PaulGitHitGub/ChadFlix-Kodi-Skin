# Releasing ChadFlix

How a release is built, checked, published and, if needed, taken back. Written for the 2026-11-14 security audit
(findings F-01, F-06, F-09, F-10).

## What a release is built from

Everything is named in `config/release.lock.json`. Nothing else is read.

| Add-on | Built from |
|---|---|
| `skin.arctic.fuse.3`, `plugin.video.pov` | The upstream archive (URL pinned to a commit, SHA-256 pinned) plus the ChadFlix changes in `inputs/overlays/<id>/` (whole files) and `inputs/overlays/<id>.removed.txt` |
| `script.chadflix.setup`, `repository.chadflix` | `src/<id>/` |
| The nine unmodified add-ons | The zip already in `zips/`, carried byte for byte (SHA-256 pinned). `tools/verify_upstream.py` compares each with its upstream archive, file by file |

No device, Kodi install, home folder or private settings file is an input.

## Steps

1. **Change the source.** Edit files under `inputs/overlays/` or `src/`. After a private assembly with
   `tools/build_repo.py`, run `tools/derive_inputs.py <id> <built zip> <upstream zip>` instead: it rewrites the overlay
   and stops unless upstream plus overlay equals the assembled add-on.
2. **Raise versions.** In `config/release.lock.json` and in the overlay's `addon.xml` (they must agree). Every add-on whose
   contents changed needs a higher version: Kodi only updates to a higher version. Never lower one, never replace a zip
   under the same version. Record the versions of the release being replaced in `previous_versions`.
3. **Build** into an empty folder, twice:
   `python3 tools/build_release.py --out A --upstream-dir <folder with the pinned upstream archives>` (and `--out B`), then
   `diff -r A B`. The two must be identical.
4. **Check:** `python3 -B -m unittest discover -s tools/tests`, `python3 tools/verify_upstream.py`, and the publication gate
   `python3 tools/validate_release.py A --private-patterns config/private-patterns.local.json`. Every check must say OK.
   A new `secrets` finding is fixed, or reviewed and added to `config/scan-allowlist.json` with a reason (one place, one
   value). `--public-only` is for CI and does not clear a release.
5. **Copy** `A/index.html`, `A/release-manifest.json`, `A/repository.chadflix-<v>.zip` and `A/zips/` into the repository,
   replacing the old `zips/` and the old repository zip. Run `python3 tools/validate_release.py . --private-patterns ...`
   once more on the repository itself.
6. **Review and publish** through a pull request; the three `release-gate` checks must pass. After Pages has deployed,
   download `zips/addons.xml` and every zip from the Pages address and compare their SHA-256 with
   `release-manifest.json`. A local build is not proof of what was deployed.

## What the hashes do and do not do

`repository.chadflix` 1.0.1 makes Kodi verify `zips/addons.xml` against `addons.xml.sha256` and every zip against its
`.sha256` file. That catches a damaged or mismatched download. It does not protect against whoever controls this
repository: they can change the files and the hashes together. Installing ChadFlix means trusting its publisher.

`zips/addons.xml.md5` stays. Installs made with `repository.chadflix` 1.0.0 only poll that file to notice a change
(they never verified it). They then find 1.0.1 in the index, update to it, and verify from then on. Do not remove the
`.md5` file while such installs may exist.

## Taking a release back

Kodi never goes down a version. To undo release N, publish release N+1 with the earlier contents under **higher** version
numbers (rebuild from the earlier overlay; change only the versions). Do not restore old zips under their old names: every
device that already updated would keep the bad version.

## Never

- Rename or delete this GitHub account or repository while anyone has ChadFlix installed: installed copies keep the old
  address. To move, first publish a higher `repository.chadflix` here that points at the new address.
- Publish with a failed or skipped check, or with `--public-only` as the only gate.
