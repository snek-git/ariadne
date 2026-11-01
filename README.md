# ariadne

Search your files by meaning, find files like a given one, and get new downloads routed into the
folders you already made. Everything runs on this machine: Qwen3-VL-Embedding-2B on the 9070 XT puts
text and pictures in one space, so "screenshot of an error message" finds screenshots.

Two parts:

- **The daemon** (`src/ariadne`, Python, uv): holds the model and the index, answers JSON on
  `127.0.0.1:8199`, watches `~/Downloads` for new files.
- **The file manager** (`cosmic-files/`, branch `ariadne`): a COSMIC Files fork whose search box
  asks the daemon first and lists results best match first, then plain filename matches. Right
  click a file, **Find similar files**. The **Inbox** at the top of the sidebar shows what the
  drop zones did: suggestions with **Move** and **Keep here**, files moved on their own with
  **Undo**, and a count of suggestions waiting. It updates live. Without the daemon it searches like
  stock COSMIC Files and the Inbox is empty. Its open and save dialogs share the same code.

What the window does on top of COSMIC Files (pills, cards, ghosts, the undo thread, noticed cards,
Tidy, the map, the palette theme), and what each workflow's test status is:
[docs/build-plan.md](docs/build-plan.md).

## Use

```
ariadne up                 # start the daemon (transient user unit, no boot service)
ariadne index              # first run, or catch up after downtime; ariadne status shows progress
ariadne find invoice from the georgia trip
ariadne find screenshot of an error message --under ~/Pictures
ariadne like ~/Documents/dnd/alamairis/Edric_Paladin_L2.pdf
ariadne route ~/Downloads/some-file.pdf
ariadne dupes
ariadne eval               # holdout test of routing on your sorted folders
ariadne down
cosmic-files/target/release/cosmic-files
```

## As the desktop's file manager

- **Folders:** `cosmic-files` is the `inode/directory` default.
- **Show in folder:** `cosmic-files --file-manager1` owns `org.freedesktop.FileManager1` on the
  session bus. Browsers and editors call it, and it opens a window per call, with the file
  selected. It is started on demand through a D-Bus service file and the user unit
  `ariadne-file-manager1.service`.
- **File picker:** `portal/` is xdg-desktop-portal-cosmic built against the fork, so its open
  and save dialogs are this window. Its `.portal` file offers FileChooser only, and
  `hyprland-portals.conf` pins it. GTK apps reach it with `GTK_USE_PORTAL=1` (GTK 3) and
  `GDK_DEBUG=portals` (GTK 4), Firefox with `widget.use-xdg-desktop-portal.file-picker=1`.

## Drop zone

A file that lands at the top of a drop zone (`~/Downloads`, `~/Downloads/Telegram Desktop`,
`~/Desktop`, set in `config.py`; their subfolders are not watched, and files already there are left
alone) is embedded once its size settles and routed:

- **Sure:** moved right away and listed in the Inbox for a day with Undo. Sure means the vote
  is at least 90% sure (right 97.5% of the time on the holdout), or Jev is at least 70% sure and
  at least 70% that a folder fits (right 89% of the time on the files the vote was unsure of).
- **Not sure:** it stays put and waits in the Inbox with Move and Keep here. From a terminal,
  `ariadne suggestions` lists them and `ariadne accept` moves the newest (`--to DIR` for another
  folder).

A download that is a copy of a file you already filed (the same bytes, or a 0.99 look-alike for
text and pictures) waits in the Inbox with the copy named, instead of moving.

Undo (the Inbox button, or `ariadne undo`) puts a move back; a file you put back is left alone
after that. Moves are logged in `~/.local/share/ariadne/moves.jsonl`, and the Inbox reads the
moved list from there, so it survives restarts. Nothing uses desktop notifications.

Moves you make yourself from one folder to another inside a root are logged too, as yours: the
file keeps its vector at the new place, so the vote learns from it, and Undo works on them as well.
A move between two roots (`~/Downloads` to `~/Documents`) reaches the watcher as a delete and a new
file, so it is not logged.

## Tidy

In the background (every 10 minutes, and a few seconds after anything moves) the daemon looks over
what you already filed and offers changes, which the window shows as cards. Local rules find the
candidates, then Opus judges each one (below). These are the rules:

- **Misfiled:** files with content under `~/Documents` or `~/Pictures` whose 10 nearest
  neighbors (each file left out of its own vote) put them in another folder, 90% or more. Files
  that share a folder and a target are one change. When more than a third of a folder's files
  point the same way, the change is about the folders, so the pair goes to the merge rules below:
  13 stills beside 2,851 frames come to nothing there, as they should. When fewer do, they make
  one card ("Move 3 files"), but only out of a folder with 3 or more files with content and into
  another project (`~/Documents/stl` and `~/Documents/invoice` are two; across projects the 90%
  is the vote for the target's whole project). Inside one project the layout is yours (a font's
  licence beside the font). Name-only files are left out: they vote by name alone. On the real
  home this finds nothing today, which is right.
- **Duplicate:** two byte-identical files; the extra copy goes to the trash. The copy outside the
  drop zones is kept, then one sorted into a folder inside a drop zone (`~/Downloads/docs`) over
  one loose at its top, then the plain name (`cv.pdf` over `cv (2).pdf`), then the older. Two filed
  copies are offered only when one is the other saved again (`x (1).png`): a vault's pasted images
  are linked by name. Same-named copies inside one project folder are left alone.
- **Merge:** two folders whose names mean the same thing, and whose parents' names do too
  (`dnd/alamairis` and `dnd/Alamairis`), with files that average to nearly the same vector: 0.6
  when the names match, 0.9 when they are a typo apart. Names that differ in a numbered word
  (`shoulder-2-watch`, `shoulder-1c-watch`) are a series, and `Artoria/textures` beside
  `JacktheRipper/Textures` is each project's own layout. The smaller moves into the larger.
- **Group** (files about one thing into a new folder) is not built.

**The judge.** Each candidate goes once to `claude -p --model opus` (no tools, no CLAUDE.md, hooks
or MCP: `--safe-mode`), in the background thread, never while the window waits. It sees the idea
in one sentence, the names in the folders involved (40 per folder, with the total) and the first
~1k characters of text where a file has some. Those leave the machine, the same way Jev's
excerpts do. Opus answers yes or no with a short reason; only yes ideas are shown, with the
reason (`reason` in `/proposals`). Verdicts are kept by idea id in `tidy.json`, so a pass never asks
twice. When `claude` is missing or a call fails, that idea stays hidden and is asked again on the
next pass: quiet, never wrong. On the real home, Opus kept 19 of the 20 candidates. It said no to
merging `dnd/alamairis` (Edric's character sheet) into the Alamairis session notes, which never
name Edric.

Every change goes through the moves log, so Undo puts it back. Not now quiets an idea for 30 days
and Never mutes a folder; both are kept in `~/.local/share/ariadne/tidy.json`.

Routing has two steps:

1. **Vote.** The 10 most similar files you already sorted vote for their folder and every folder
   above it, weighted by similarity. The answer is the deepest folder holding 60% of the vote; that
   share is the confidence.
2. **Decide with Jev.** When the vote is under 90% sure and the file has text, one request to Jev
   (TypeSafe) asks a Choice among the vote's top 5 folders and a Noul for "does any of these fit".
   Each folder is described by its path, a few file names and its subfolders, or by your own line
   in `~/.config/ariadne/folders.txt` (`~/Documents/invoice = monthly contractor invoices`). Jev
   sees the file name and its first ~1k tokens of text. Pictures and name-only files stay with the
   vote. The key comes from `TYPESAFE_API_KEY` or `~/.config/typesafe/api_key`; without one the vote
   decides alone.

Holdout test (`ariadne eval`, `ariadne eval --jev`): hide 15% of your sorted files (1,057), route
each, count how many land back where they were.

| | Exact folder | Right folder or a parent |
| --- | --- | --- |
| Vote alone | 56.3% | 88.8% |
| Vote, Jev on unsure text files (161 calls) | 60.7% | 89.5% |

On the unsure text files alone Jev is right 79.5% of the time vs 75.2% for the vote; when it gives
its pick 70% or more it is right 89% of the time. Jev on name-only files was much worse than the
vote (26% vs 68%), which is why they skip it.

## What gets indexed

Roots: `~/Documents ~/Downloads ~/Pictures ~/Desktop ~/Videos` (`config.py`).

| Kind | Files | Embedded |
| --- | --- | --- |
| text | md txt rst org tex html srt pdf | the name, and the whole text in ~1k-token chunks |
| picture | png jpg webp gif bmp | the name, and the picture (scaled to 480 px) |
| name only | everything else (stl, 3mf, zip, mp4, csv, json, log, HEIC...) | the name |

Skipped: hidden files and folders, `node_modules`, `__pycache__`, venvs, `target`/`dist`/`build`,
source code and compiled objects, game containers, `.pem`/`.key`, partial downloads,
`~/Documents/Claude`, `~/Documents/Codex`, `~/Pictures/h`.

Data files (csv, json, log) are name-only on purpose: the biggest is a 224 MB CSV that would be
64k chunks of numbers.

Index: `~/.local/share/ariadne/index.db` (SQLite, float16 vectors). Search loads it into numpy.

While the daemon runs it watches every root: new and edited files are embedded once they stop
growing, moved or renamed files and folders keep their vectors, deleted ones leave the index.
`ariadne index` is only needed for the first run or to catch up on changes made while it was down.
