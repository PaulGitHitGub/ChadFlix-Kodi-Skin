# Changelog

## Release 7 (2026-11-17)

Kodi installs this update by itself (or **Add-ons** → **Check for updates**). **Restart Kodi once afterwards.** You
don't need to run ChadFlix Setup again: Setup 1.0.6 updates its two changed key files by itself.

**Versions:** Arctic Fuse 3 **3.99.418.7**, POV **6.99.1103.6**, ChadFlix Setup **1.0.6**. ChadFlix Repository (1.0.1) is
unchanged.

### Added

- **Next Episode** card: near the end of an episode a card with a picture of the next episode counts down and plays it
  by itself.
- **Remove From Row:** hold **OK** on a title in **Continue Watching** or a favorites row to take it off that row.
- **Save To Watchlists:** a new picker on the movie page for choosing which of your watchlists a movie is on.
- **All Sources** on the show page: press **Right** from **Resume** (or **Play**) to open an **All Sources** side menu,
  the same look as on the movie page.

### Changed

- Quality picks: **Highest** is the top source in **All Sources**. **Medium** is the first confirmed HDR or Dolby Vision
  file in the **All Sources** list that isn't a remux (Dolby Vision preferred). **Play** starts **Medium**.
- Smoother scrolling: TMDb Helper pauses its extra lookups while you browse rows.
- **All Sources** lists also show **Unchecked** rows: sources Real-Debrid hasn't confirmed as cached, at the bottom of
  the list with a grey **Unchecked** line. **Play**, the quality picks and the next-episode preparation are unchanged.
- The plot on the Home rows is now cut short to one line (a plot with its own line break shows two). The full plot is still on the movie and show pages. Holding
  **Left** or **Right** on Home now scrolls much more smoothly (about 40 → 57 frames per second on a Shield).
- The lines of remote-button hints are gone from the gear menu, the subtitle style picker, the Showing tile and the
  source info screen.
- ChadFlix Setup asks "Close Kodi?" before it switches the look, so the question can no longer vanish while the new look
  loads. YouTube is asked about once (Kodi's own question).

### Fixed

- Stability fixes.

### Known trade-offs

- A short press of **OK** reacts about 0.1 s later than before (Kodi waits to tell a short press from a hold).
- The next episode starts 3–4 seconds after the countdown ends.
- **Play** now starts a much bigger file: usually about 31–37 GB instead of about 5–6 GB (**Highest** is usually a
  60–90 GB remux). On a slower connection you will notice.
- On a movie page the ratings appear 0.2–3 seconds after the page opens, and the logo and title move up slightly when
  they do.
- **Menu** and **C** no longer open **Add to Another Watchlist** on a movie page. Holding **OK** on **Watchlist** still
  does.
- The Home rows no longer show the whole plot, only its first line.
- An **Unchecked** row may not play. After a few seconds you get "No Results" and the page comes back. The "N sources"
  line under **All Sources** on a movie page still counts confirmed sources only.

## Release 6 (2026-11-16)

Kodi installs this update by itself (or **Add-ons** → **Check for updates**). **Restart Kodi once afterwards.** You
don't need to run ChadFlix Setup again.

**Versions:** Arctic Fuse 3 **3.99.418.6**, ChadFlix Setup **1.0.5**. POV (6.99.1103.5) and ChadFlix Repository
(1.0.1) are unchanged.

### Fixed

- Kodi shows one continuous splash while it starts, without flashes or black frames in between.
- New installs get the ChadFlix home rows. ChadFlix Setup 1.0.5 adds the row files only when they are missing or
  empty; it never replaces a row file you already have.

## Release 5 (2026-11-15)

Kodi installs this update by itself (or **Add-ons** → **Check for updates**). **Restart Kodi once afterwards.** You
don't need to run ChadFlix Setup again.

**Versions:** Arctic Fuse 3 **3.99.418.5**, POV **6.99.1103.5**, ChadFlix Setup **1.0.4**. ChadFlix Repository
(1.0.1) is unchanged.

### Changed

- Press **Back** twice quickly on the **Movies**, **Shows** or **My Lists** tab to open **Video add-ons**. One **Back**
  from there returns you to the page you came from.
- The **Trending** row on **Movies** and **Shows** is now headed **All Genres · Trending**.
- ChadFlix Setup 1.0.4 updates its **Back** key file by itself.

### Fixed

- Stopping an episode brings back the show's page instead of the **Shows** tab.
- Rows also wait for POV requests they used to miss before they start loading (for example right after you stop a video).

## Release 4 (2026-11-15)

Kodi installs this update by itself (or **Add-ons** → **Check for updates**). **Restart Kodi once afterwards.** You
don't need to run ChadFlix Setup again.

**Versions:** Arctic Fuse 3 **3.99.418.4**, POV **6.99.1103.4**, ChadFlix Setup **1.0.3**, ChadFlix Repository **1.0.1**.

### Changed

- New **My Lists**: your lists as cards, with **Saved Movies** and **Saved Shows** rows. Save single episodes from a
  show's episode list; they show under **Saved Episodes**.
- Grids fetch their pages together instead of one by one.
- Hold **OK** on **Watchlist** on a movie page to add the movie to another list.

### Fixed

- Grids no longer stall after a video ends.

### Security

- Security hardening: sign-in secrets are masked in Kodi's log, sign-in QR codes are drawn on the device (TMDb Lists
  now shows a QR code), playable links are no longer kept on disk, and Kodi checks every download from this repository
  with SHA-256.
- Installed before release 4? Set **Update official add-ons from** back to **Official repositories only
  (default)**, then turn **Unknown sources** off (README, Install step 6).

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
