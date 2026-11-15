# Changelog

## Release 4 (security fixes; staged 2026-11-15, not published yet)

Kodi installs this update by itself. **Restart Kodi once afterwards.** You don't need to run ChadFlix Setup again.

**Versions:** Arctic Fuse 3 **3.99.418.4**, POV **6.99.1103.4**, ChadFlix Setup **1.0.3**, ChadFlix Repository **1.0.1**.

Fixes from the 2026-11-14 security audits. Nothing changes in how the app looks or is used, except the TMDb Lists sign-in
screen (it shows a QR code and the site name instead of a short link).

- Sign-in secrets are masked in Kodi's log, and the optional log upload is cleaned before it is sent.
- Sign-in QR codes are drawn on the device; no link or code goes to a QR image service, and the TMDb sign-in link no
  longer goes through a link shortener over plain HTTP.
- Playable links are no longer kept on disk after a source check; old ones are deleted. Sign-in values are no longer
  copied into a Kodi window property.
- Calls to debrid and list services give up after a few tries when the service is unreachable.
- ChadFlix Setup answers only Kodi's own "Keep skin?" question.
- ChadFlix Setup installs a fifth key file (the Watchlist menu key added in release 3). If you already have ChadFlix, it
  is added by itself at the next Kodi start. The menu entry is now **Undo ChadFlix Keymaps**.
- Kodi now verifies every download from this repository with SHA-256.
- After installing, set **Update official add-ons from** back to **Official repositories only (default)**, then turn
  **Unknown sources** off (README, Install step 6).

## Release 3 (2026-11-14)

Kodi installs this update by itself (or **Add-ons** → **Check for updates**).

**Versions:** Arctic Fuse 3 **3.99.418.3**, POV **6.99.1103.3**. ChadFlix Setup (1.0.2) and ChadFlix Repository
(1.0.0) are unchanged.

### Changed

- **Favorite** and **Watchlist** buttons toggle, with no confirmations or pop-ups. On a show's page, hold **OK** on
  **Watchlist** to add the show to another list (movie pages get this in the next update).
- New look for the **Showing** picker and the genre menu. The genre menu follows your home order.

### Fixed

- Grids no longer get stuck on grey cards after a video.
- Small fixes: the genre menu grid no longer jumps, **More Like This** shows a grey loading card, and the scrub bar
  reads **Jump To**.

## Release 2 (2026-11-14)

Kodi installs this update by itself (or **Add-ons** → **Check for updates**). **Restart Kodi once afterwards.** You
don't need to run ChadFlix Setup again; if Back misbehaves, open it once.

**Versions:** Arctic Fuse 3 **3.99.418.2**, POV **6.99.1103.2**, ChadFlix Setup **1.0.2**. ChadFlix Repository stays
at 1.0.0.

### New

- **Showing Tile**: every genre row starts with a Showing tile. Pick Trending (Default), New Releases, Blockbusters,
  Most Watched or Top Rated (Shows: New Series and Complete Series instead of New Releases and Blockbusters). Each row
  remembers its choice.
- **Customize Home**: gear → Customize Home. Turn rows on or off and reorder them, separately for Movies and Shows.
  Rows that are off don't load.
- **Anime Row**: replaces Animation. Its first tile is a Genre tile that changes the row in place.
- **Continue Watching**: the first row on Movies and Shows, with a progress bar and the time left or the next episode.
- **Subtitles**: pick the subtitle size and style, with a live sample line.

### Changed

- **Play** prefers the best 4K source (Dolby Vision or HDR first).
- **Skip Intro**: one pop-up when the intro starts.
- Fewer add-on scripts start at the same moment, which was behind most "Kodi closes on its own" reports.

### Updating From the First Release

- POV's **Reuse Language Invoker** (and its Fix setting) is switched off once, because it caused crashes on Android
  TV. To turn it back on: POV → Tools → Set Language Invoker. ChadFlix won't switch it again.
- ChadFlix Setup 1.0.2 updates the remote-control files by itself (backing up the old ones and leaving files you
  edited alone), and **Undo ChadFlix keymaps** also recognises the files from the first release.

### Known Issues Added

- On Movies, the Documentary, Mystery and Romance rows can show fewer titles on Trending, without **More Titles**.
- **More Titles** grids are slower the first time you open them each day.

## 1.0.0 (2026-11-12): first release

- **ChadFlix Repository 1.0.0** and **ChadFlix Setup 1.0.1**.
- Arctic Fuse 3 **3.99.418.1** and POV **6.99.1103.1** (unofficial modified builds): the look as of release
  R8.6. Movies / Shows / My Lists, genre and Trending grids, audio and subtitle cards, ±10 s skip, exit
  question, "Ends at" clock, Options rows for Real-Debrid, ratings and the MDbList key.
- The setup installs and enables the add-ons, applies the layout settings (no skin setup wizard), switches the
  skin after asking (it answers Kodi's "Keep skin?" itself, because Skin Variables reloads the skin on its first
  start and closes that question), adds four remote-control files and backs up anything it replaces.
  Safe to run again.
- Setup 1.0.1: a first menu with **Set up ChadFlix** and **Undo ChadFlix keymaps** (removes the four key files and
  puts back anything they replaced, for going back to another skin); clearer message when you choose "Not now";
  waits up to 60 s for the skin to load on slower boxes.
