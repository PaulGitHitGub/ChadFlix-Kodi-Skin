# Notice: credits, licences and changes

**This is an unofficial, modified build.** It is not made, endorsed or supported by the original authors.
Please report problems here, not to them.

## What is in this repository

| Add-on | Version here | Author | Licence | Changed here? |
| --- | --- | --- | --- | --- |
| Arctic Fuse 3 (`skin.arctic.fuse.3`) | 3.99.418.1 (based on 3.4.18) | jurialmunkey | CC BY-NC-SA 4.0 | **Yes**, see `CHADFLIX-CHANGES.txt` inside the add-on |
| POV (`plugin.video.pov`) | 6.99.1103.1 (based on 6.11.03) | kodifitzwell | GPL-3.0 | **Yes**, see `CHADFLIX-CHANGES.txt` inside the add-on |
| TMDb Helper (`plugin.video.themoviedb.helper`) | 6.20.8 | jurialmunkey | GPL-3.0-or-later | No |
| TMDbHelper Sync - MDbList (`script.module.tmdbhelper.sync.mdblist`) | 1.1.4 | jurialmunkey | GPL-3.0-or-later | No |
| Skin Variables (`script.skinvariables`) | 2.2.12 | jurialmunkey | GPL-3.0-or-later | No |
| Texture Maker (`script.texturemaker`) | 0.2.11 | jurialmunkey | GPL-3.0-or-later | No |
| jurialmunkey common (`script.module.jurialmunkey`) | 0.2.35 | jurialmunkey | GPL-3.0-or-later | No |
| InfoTagger (`script.module.infotagger`) | 0.1.0 | jurialmunkey | GPL-3.0-or-later | No |
| Arctic Splash (`resource.images.arctic.splash`) | 1.1.0 | jurialmunkey | none stated | No |
| Arctic Weather (`resource.images.arctic.weather`) | 1.0.4 | jurialmunkey | none stated | No |
| Roboto CJKSC (`resource.font.robotocjksc`) | 0.0.3 | jurialmunkey (font: Google) | Apache-2.0 | No |
| ChadFlix Setup (`script.chadflix.setup`) | see repository | ChadFlix | GPL-3.0-or-later | New |
| ChadFlix Repository (`repository.chadflix`) | 1.0.0 | ChadFlix | GPL-3.0-or-later | New |

The unchanged add-ons are identical, file for file, to the authors' published code: TMDb Helper, Skin Variables,
jurialmunkey common and Roboto CJKSC to their release tags (v6.20.8, v2.2.12, v0.2.35, v0.0.3); Texture Maker,
InfoTagger, Arctic Splash and Arctic Weather to the current main branch of their repositories; the MDbList module
to commit 944c37a. They are hosted here only because Kodi's own repository has older versions than the skin needs
(or none). Everything else (requests and its libraries, qrcode, addon.signals, AutoCompletion, studio and weather icons, YouTube) comes from Kodi's official repository.

## Credits

- **jurialmunkey**: Arctic Fuse 3, TMDb Helper, Skin Variables, Texture Maker, the jurialmunkey and InfoTagger
  modules, the MDbList sync module and the Arctic image and font resources.
  https://github.com/jurialmunkey
- **kodifitzwell**: POV. https://kodifitzwell.github.io/repo/
- Fonts bundled in Arctic Fuse 3 keep their own licences (for example `fonts/LeagueSpartan-OFL.txt`).

## Changes made here

- **Arctic Fuse 3**: Netflix-style Movies / Shows / My Lists pages, genre and Trending grids, title and show
  pages, audio and subtitle cards, ±10 s skip, exit question, "Ends at" clock, Options rows for Real-Debrid,
  ratings and the MDbList key, and supporting scripts in `extras/`. Changed November 2026. The exact list of
  changed and added files is in the add-on's `CHADFLIX-CHANGES.txt`. One comment in `extras/netflix_profiles.py`
  had a personal name replaced; its code is unchanged.
- **POV**: catalogue rows and grids for the skin, faster page loading, source and quality presentation, and
  playback return paths. Changed November 2026. List in the add-on's `CHADFLIX-CHANGES.txt`. POV's built-in
  default settings (`resources/settings.xml`, which holds POV's own public default ids) are unchanged.
- Both: version numbers raised (see README, *Updating*) and an "unofficial modified build" line added to the
  add-on description.

## Licences

- GPL-3.0 text: [LICENSES/GPL-3.0.txt](LICENSES/GPL-3.0.txt). The changed POV carries a copy as `LICENSE.txt`.
  The Python source is the program itself, so the full corresponding source is in each zip.
- Arctic Fuse 3 is under **Creative Commons Attribution-NonCommercial-ShareAlike 4.0**
  (https://creativecommons.org/licenses/by-nc-sa/4.0/): the changed skin is shared under the same licence,
  credited to jurialmunkey, marked as changed, and must not be used commercially. This repository is free and
  non-commercial.
- **Arctic Splash and Arctic Weather** are jurialmunkey's artwork and state no licence. They are copied here unchanged
  from the author's public repository, with credit, only so the skin installs in one step. **They will be removed on
  request** of the author; the skin would then fetch them from the author's own repository.
