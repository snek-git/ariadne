# Build plan: ariadne in COSMIC Files

What has to exist in the fork (`cosmic-files/`, branch `ariadne`) and the daemon (`src/ariadne/`)
for every workflow in the combined prototype
(<https://claude.ai/artifact/EsthurCYyJRgLUoxutjU3N>, `prototypes/combined/index.html`).
Each numbered row is also a test: the workflow passes when it works end to end in the headless
stage (`demo/stage`) against the real daemon.

Status words: **Tested** (works end to end in the stage against the live daemon), **Built**
(in the fork, not seen end to end), **Later** (planned, not started), **Not in iced** (the
prototype does it, the toolkit makes it costly; the row says what we do instead).

## Ground rules

- Everything lives in the window. No desktop notifications, no CLI flows.
- Keep the COSMIC skeleton: header, sidebar, tabs, grid and list views. Change the rest.
- Short labels. No captions, no explanation text, key hints only behind `?`.
- The user always knows what ariadne is doing: status line, knots, ghosts.
- The fork stays a thin client. All thinking happens in the daemon on `127.0.0.1:8199`; every
  call degrades to plain COSMIC Files when the daemon is down (seen in the stage during two
  daemon restarts).
- Grow in layers: each row lands on a fork that already works.

## Where it lives in the fork

| File | What |
|---|---|
| `src/ariadne.rs` | the daemon client: snapshot, marks, proposals, peek, summaries, sizes |
| `src/ariadne_ui.rs` | pill, card, ghost tile, noticed card, summary editor |
| `src/palette.rs` | the palette file as a COSMIC theme, the tones for the fork's own widgets, the font and icon theme, the one-ink icon tint |
| `src/yorha.rs` | the shell's drawn parts under their QML names: DotRail, CursorLozenge, GridTexture, DiagonalOrnament, tracked caps, TitleLockup (demo: `cargo run --example yorha_demo`) |
| `src/map.rs` | the treemap canvas widget (demo: `cargo run --example map_demo`) |
| `src/tab.rs`, `src/app.rs` | wiring: overlays in the grid, footer, the Tidy and Settings panels, map view, key bindings |

## A. Arrivals

| # | Workflow | Status | Notes |
|---|---|---|---|
| A1 | A waiting file in a drop zone wears a pill: `→ folder  30%` | Tested | grid and list; long folder names are cut to fit |
| A2 | Click the pill: card with the folder, Move, Keep, More | Tested | Move ran the real move |
| A3 | More: the vote's other folders with %; pick one to move there | Tested | Other folder, New folder and Always do this are Later |
| A4 | A rename offer on the card | Later | needs `suggested_name` from a local model |
| A5 | A download you already have: `copy` pill, card says the folder it is already in, Drop copy | Tested | Drop copy uses COSMIC's own trash, so it can be restored |
| A6 | The card says where the file came from | Tested | origin xattr host, else the drop zone's name |
| A7 | `reading ▮` while ariadne decides | Built | too quick to catch in the stage |
| A8 | A file moved on its own leaves a ghost tile with Undo | Tested | Undo put the file back |
| A9 | Where it landed it wears a `NEW` plate; click for Undo / OK | Built | |
| A10 | Several waiting files: a `WAITING  03` bar under the items with Move all / Keep all | Tested | Keep all on three test files cleared them all and the bar went with them; copies are left to their own card |
| A11 | Next on that bar, or Alt+N, puts the cursor on the next waiting file | Tested (Next), Built (Alt+N) | a plain N would steal the letter from type-to-search; the stage's virtual keyboard drops every Alt combo (Alt+Up too), so the key is unseen there |
| A12 | Count dots: a folder tile counts the files waiting anywhere under it (solid, the one dark), and its Tidy ideas while Tidy is open (ink outline); a place in the sidebar shows the waiting count after its name | Tested | |

## B. Knowing what happens

| # | Workflow | Status | Notes |
|---|---|---|---|
| B1 | Undo thread: one knot per move; click a knot for its card with Undo | Tested | |
| B2 | Ctrl+Z undoes the newest ariadne move | Tested | your own moves are skipped by design |
| B3 | A move you make by hand gets a `learned` knot | Tested | moves inside one root only; Downloads to Documents reaches the watcher as delete + new |
| B4 | Live status line, typed out at 18 ms a character with `▮` | Tested | `moved e2e-errand-list.txt to Artoria Lancer - Fate Go` |
| B5 | Toasts with Undo | Built | Undo shows a toast; the card actions do not toast yet |

## C. Tidy

| # | Workflow | Status | Notes |
|---|---|---|---|
| C1 | ariadne noticed: one card per folder visit, sure ideas only, the files involved get an ink frame | Tested | |
| C2 | Card buttons: the action, Not now (30 days), Never (mute the folder) | Built | daemon side tested by curl; not pressed in the stage, to keep real ideas alive |
| C3 | Tidy: a panel beside the items (header slab `TIDY  19`, Do all, Close), the ideas by kind; each a plate with its verb as a tag on the gold edge, how sure, the file, `from → to`, Opus's reason, its own button and Skip | Tested (view) | not applied: these are real changes to real files. Seen with 42; now 19. The detail panel steps aside while it is open |
| C4 | Kinds: misfiled, duplicate, merge | Built | the rules find 20 on the real home (19 duplicates, the Alamairis merge, no misfiled; faked spell notes in `invoice` are caught, 3 with one target as one "Move 3 files" card). When most of a folder points one way, the folder pair goes to the merge rules instead: 5 such pairs today (stills/frames among them), none passes |
| C5 | Opus judges each candidate once (`claude -p`), only yes ideas show | Tested (daemon) | kept 19 of 20, said no to the Alamairis merge; claude missing or failing shows nothing |

## D. Look-ups

| # | Workflow | Status | Notes |
|---|---|---|---|
| D1 | Search by meaning, full-width bar | Tested | split into By meaning / By name is Later |
| D2 | Like this | Tested earlier | the constellation is Later |
| D3 | Peek: the detail panel shows a line from the selected file under `PEEK` | Tested | facts and rename need extraction (Later). Space is COSMIC's gallery, and a focused tile takes it first, so the ledger no longer offers it |
| D4 | Pointer lens | Later | |
| D5 | Folder summary: right-click, Summary, type, Enter; tooltip on hover | Tested (edit), Built (hover) | tooltips do not show in the headless stage at all |
| D6 | Drag glow | Later | |
| D7 | Collections | Later | needs clustering in the daemon |

## E. Map

| # | Workflow | Status | Notes |
|---|---|---|---|
| E1 | Map view (View menu, Ctrl+3): folders as a treemap by disk use | Tested | Documents, Downloads, Pictures and Desktop under one Home; all of home takes minutes to measure |
| E2 | Meaning on top: misfiled arrows, dashed merge borders | Tested (merge) | collection outlines need D7 |
| E3 | Click to zoom, click again to open the grid | Tested | wheel zoom built, not tested (the virtual pointer has no wheel) |
| E4 | Moves travel across the map; clicking a knot replays its route | Tested (travel) | a real move from Downloads into the Obsidian vault ran its dot and ring. The map stops three levels down, so a travel or a misfiled arrow lands on the nearest folder it draws (`screen_near`) |

## F. Look and feel

| # | Workflow | Status | Notes |
|---|---|---|---|
| F1 | The window wears `~/.config/yorha/palette.toml`, read at runtime, no hex in the code | Tested | treatment C, round 2 (see the tone list below) |
| F2 | Edit the palette file and the window follows | Tested | both an in-place edit and a new path |
| F3 | Settings: Theme (Match desktop, Dark, Light, Palette), Palette file, Font, Icons, Motion, Sound | Tested (view) | stored in COSMIC's own config; Font defaults to Monaco, Icons to Ariadne (F11). Under the palette Settings is a panel beside the items, as Tidy is, with micro caps section rules in place of COSMIC's bold titles |
| F4 | Motion: typed status line; the selection's arrival flash | Tested | the flash is the manifest's: `accent`, `border_mid` from 25% to 87.5% of 800ms, `accent`, once per cursor move, in the list rows and the grid's names. Stepped reveals are Later |
| F5 | Sound at the action chokepoints | Later | the toggle exists; samples are `~/.config/quickshell/sounds` |
| F6 | Texture: 7px grid at 4%, dot-triad rails under the header and over the ledger | Tested | drawn by `src/yorha.rs`; the diagonal ornament is built but left out: under MSAA iced_wgpu paints a lower `stack` layer over the panels above it, and only thin gutters of field show anyway |
| F7 | `?` shows the keys | Later | the ledger already lists the main keys |
| F8 | The launcher's sheet: places panel and items panel with header slabs (`DOCUMENTS  24 / 32`), a `WHERE` well, a detail panel (plate header, icon in a matte, title lockup with its ghost, label and value rows, tags with the gold edge, ariadne's line under `PEEK`, Open), the ledger in micro caps | Tested | detail panel shows above 900px of width and hides under the map. The popups (arrival, copy, new, knot, summary) are the same sheet: header slab with the time, label and value rows, alternatives as plate rows, 2px shadow and a 1px frame. Pills are one line of micro caps; the ghost tile has an `UNDO` plate; the noticed bar opens with a `NOTICED` slab. The list keeps a margin where the cursor lozenge marks the focused row; on the selection the cells and the icon take the light ink. Text is the law's 12px (the fork's body text and the app default; COSMIC's own menus keep theirs); times read `2026-09-24 01:11` or `today 01:08`, never the locale's month names. A search's header names it (`SEARCH  INVOICE`); an empty view says so in micro caps (`NOTHING NEW`) |
| F9 | Icons in one khaki ink, the launcher's TintIcon curve (floor 0.2, gamma 1.6, per-icon stretch); bitmaps scaled by whole steps; thumbnails keep their colours inside a framed matte | Tested | |
| F10 | Previews as Finder shows them: the detail panel opens with a big matte holding the photo at full size, the thumbnail a thumbnailer drew, or the first lines of a text file. The grid shows pictures only for photos, video, fonts, EPUB covers and PDFs with a real cover; text, code, 3D and text-page PDFs keep their icon there and show only in the panel | Tested | `text_page` and `detail_preview` in `tab.rs`; text ink from the ladder. Panel-only pictures are `ItemThumbnail::Preview`; a PDF counts as a cover when 40% of its first page is inked (`is_cover` in `thumbnailer.rs`, tuned on 59 of snek's PDFs: covers 46% and up, text pages 33% and under). PDF, STL, EPUB and fonts come from system `.thumbnailer` files: `papers`, `f3d`, `gnome-epub-thumbnailer`, `gnome-font-viewer`. Exec codes inside an argument (f3d's `--output=%o`) are now filled in, and the output file ends in `.png`. Slicer 3MFs (Bambu, Orca) keep the mesh in part files that f3d cannot read, so the fork lifts the slicer's own render out of the archive (`embedded_3mf` in `thumbnailer.rs`); a 3MF with neither keeps its icon. Archives keep their icon |
| F11 | Our own icons, the Ariadne theme: a page with its corner cut at 45° and a solid foot strip with the list bullet, folders as gray plates with the rail's dot triad; each kind a pictogram in the game's grammar (head dot, diamonds, chevrons, arcs, the open ring, the Skills brackets, the fusion chip) | Tested | drawn by `icons/build.py` (grayscale on a 16 unit grid, hairline frames; the fork's tint inks it); installed to `~/.local/share/icons/Ariadne`, which falls back to hicolor for anything not drawn. The sidebar's marks are the same glyphs redrawn at one unit a pixel as symbolic icons (Inbox a chevron into a tray, Recents the System ring with a hand, Networks the Quests cluster joined); the header's search and window buttons are still the system's |

Under any other theme the fork is plain COSMIC Files again: its own nav bar, font and icons.

Tones the palette theme spends, each with its element (manifest section 2, PRIMITIVES.md round 2):
`base` the field; `highlight` panel bodies; `base_dim` row plates, tags, wells, the ornament;
`shadow` the 2px panel shadow and section rules; `accent` header slabs, the current place, the
selected item; `highlight_2` light ink on those; `text` ink, frames, the grid at 4%, rail rules;
`ramp.dot` rail staples and triads; `ramp.selected` the solid square on a plate header and the
cursor lozenge; `border_mid` secondary text, labels, the ledger; `text_faint` the title ghost;
`extras.gold` the category edge on a kind tag; `urgent` the NEW plate.

## Daemon API

All JSON on `127.0.0.1:8199`. `/inbox`, `/moves`, `/activity` and `/proposals` share one
`version` (it starts at the boot time, so a restart always reads as a change); the fork polls
`/inbox?version_only=1` every 2 s and `/activity` every second.

```
GET  /inbox            {"version", "items": [{"state": "suggested"|"moved"|"reading", "path", "folder",
                         "share", "fits", "by", "from", "at", "source", "dup_of", "alternatives": [{"folder", "share"}]}]}
GET  /moves?limit=40   {"version", "moves": [{"id", "at", "from", "to", "by": "ariadne"|"you", "share", "learned"}]}
GET  /activity         {"version", "line", "busy"}
GET  /proposals?folder={"version", "proposals": [{"id", "kind": "misfiled"|"duplicate"|"merge"|"group",
                         "confidence", "paths", "from", "to", "name", "reason"}]}   (Opus said yes)
POST /proposals/apply {"id", "name"?}    POST /proposals/skip {"id"}    POST /mute {"folder"}
GET  /summary?path=    {"path", "text"}  POST /summary {"path", "text"}   (~/.config/ariadne/folders.txt)
GET  /peek?path=       {"kind", "excerpt", "facts", "rename"}
GET  /sizes?under=&depth=3   {"path", "name", "size", "kids"}   ({"pending": true} while measuring)
POST /accept {"path", "folder"?}   POST /keep {"path"}   POST /undo {"path"?}
```

## Next

1. The rest of the arrival card (A3 extras, A4); the knot replay on the map seen in the stage.
2. D1 split, D4, D6, D7, F5, F7; COSMIC's menus and dialogs in the sheet's terms.
3. Misfiled across one big project: the whole Obsidian vault counts as one, so a note filed in
   the wrong part of it is never questioned. Needs a sharper test than the vote (Jev, or the
   folder's own words) before it can speak there.
