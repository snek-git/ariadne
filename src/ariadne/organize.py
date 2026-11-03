"""Organize as a plan: sort the files you picked the way you already file things, and show it before
anything moves. The daemon routes, moves and remembers; this module only decides.

Each file takes the first rule that fits:
1. a file in a folder you made stays, unless it is a clear stray (the Downloads piles sorted by type
   are no folder you made: their files are loose);
2. a file with a copy elsewhere, or a copy of another picked file, stays;
3. camera photos and videos go to a folder per year, screen recordings beside your screenshots' twin;
4. art saved from pixiv or twitter goes to its own folder under Pictures/art;
5. a CV goes to Documents/papers/cv (other people's to cv/others);
6. a name that points at one of your folders, an app default you keep a folder for ('Screenshot'),
   or a type you keep in one place sends the file there;
7. files that share a name word go together, into a folder most of them already fit or a new one;
8. pictures saved the same day at the same size are one set (a game's sprites, a comic's pages);
9. the rest follow their own routing decision, which never picks a Downloads pile. A document with
   paper evidence in its name (visa, statement, your name) goes to Documents/papers.
Versions of one name ('CLAUDE (2).md', 'CV-1.pdf') then share the best one's fate."""
import filecmp
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image

from . import config, decide, engine, watch
from .extract import DEFAULT_NAME, ext, family, generic, kind
from .index import wanted

GROUP = 3  # different names sharing a word before they form a group
TYPE_HOME = 3  # filed files of one type before "the type lives in one place" counts
PARENT_DEPTH = 2  # a new folder's parent sits at most this deep below a root
ART_TOPIC = 5  # saved art on one topic gets a folder of its own from this many pictures
TOPIC = 4  # letters a word needs to name a new folder ('str' does not)
LONG = 30  # pages: a document this long is a book or a lecture, not a paper
# Words that name nothing: "x_and_1_more_drawn_by_y" is how downloaded art is named.
FILLER = {"and", "the", "for", "with", "from", "more", "drawn"}
SHORT = {"cv", "id"}  # two-letter words that do name a topic
# A document named with one of these is a paper you keep; CVs get a folder of their own.
# 'քաղվածք' is a bank statement in Armenian.
PAPER = {"passport", "visa", "id", "cv", "resume", "statement", "agreement", "contract", "registration",
         "inheritance", "certificate", "insurance", "tax", "հարկ", "settlement", "release", "appointment", "letter",
         "քաղվածք"}
# Words only your own papers carry. A paper word without one of these or your name ('Action Plan on
# Visa Liberalisation', a policy paper) is proposed for papers but not ticked.
PERSONAL = {"passport", "id", "registration", "inheritance", "certificate", "insurance", "tax", "հարկ", "account"}
# The paper words that still count when only a document's text says them, beside your name. The
# rest are everyday words there: invoices name a contract and tax, bookings an id, orders a release.
SAID = {"passport", "visa", "քաղվածք"}
# Coursework and writing: 'Capstone_Paper_FeliksMarkosyan.pdf' carries your name but is no paper you keep.
ACADEMIC = {"paper", "thesis", "capstone", "essay", "assignment", "report"}
FRONT = 2  # pages of a PDF read for your name and paper words
CV = {"cv", "resume"}
# Folder names that say how a folder is sorted, not what it is about: a file's name never points there
# ('images.jpg' is not for stl/mrboss/images).
PLAIN = {"images", "image", "out", "work", "variants", "src", "output", "files", "docs", "misc", "new", "temp", "tmp"}
PARTIAL = {"part", "crdownload", "tmp"}  # unfinished downloads: never moved
UNREAD = "ariadne does not read this kind of file"
UNSURE = {"not sure where this goes", "nothing you filed looks much like it"}  # a version may still follow its kin
# A picture needs a close sibling to be placed at all: unrelated pictures still score about 0.6, so
# a vote of weak neighbours can be unanimous (game sprites voted 100% for the render frames of a 3D
# print, the biggest picture folder there is).
NEAR = decide.NAME_FLOOR
GUESS = 0.5  # an unsure one-off below this share is no suggestion
ROUNDS = 3  # times the leftovers are routed again against the folders the plan fills
SMALL = 10  # a selection this small borrows same-word siblings from beside its files to form groups
PICTURES = config.HOME / "Pictures"
PHOTOS = PICTURES / "photos"
ART = PICTURES / "art"
PAPERS = config.HOME / "Documents/papers"  # documents you keep: CVs, IDs, visas, statements
VIDEOS = config.HOME / "Videos"
RECORDINGS = VIDEOS / "screen-recordings"
FALLBACK = config.HOME / "Documents"  # where you keep projects
HOMES = {"image": ART, "doc": PAPERS, "video": VIDEOS}  # where a type lives when its only match is in Downloads
# '20261031_215145.heic', 'IMG_20250629_123456.jpg', 'PXL_20240106_165902664.jpg'
CAMERA = re.compile(r"(?:(?:img|vid|pxl|mvimg|pano)_)?(20\d\d)(\d\d)(\d\d)_\d{6}")
TIFF = re.compile(rb"Exif\0\0(?=MM\0\*|II\*\0)")
HEAD = 1 << 18  # bytes searched for the EXIF block of a picture Pillow cannot open (HEIC)
PIXIV = re.compile(r"\d+_p\d+")  # '102040501_p0'
TWITTER = re.compile(r"(?=.*[a-z])(?=.*[A-Z])[\w-]{15}")  # 'G1Os7WSbQAIucy_'
BOORU = re.compile(r"__.+_drawn_by_.+__[0-9a-f]{32}|[0-9a-f]{32}")  # tagged, or saved by its md5
SCREEN = re.compile(r"screen[-_ ]?\d")  # 'screen-2026-10-05-153131.mp4'
HEX = re.compile(r"[0-9a-f]{12,}")
CAMEL = re.compile(r"[A-Z]?[a-z]+(?:[A-Z][a-z]+)+(?:[A-Z]{2,})?|(?:[A-Z][a-z]+)+[A-Z]{2,}")  # BoxLab, SergeyKirakosyanCV
NOUNS = {"image": ("picture", "pictures"), "video": ("video", "videos"), "audio": ("audio file", "audio files"),
         "archive": ("archive", "archives"), "pdf": ("PDF", "PDFs"), "doc": ("document", "documents")}


def files(n: int) -> str:
    return f"{n} file{'s' * (n != 1)}"


def plural(f: str) -> str:
    """A type family as you would say it: 'videos', '.fish files'."""
    return NOUNS.get(f, (f".{f} file", f".{f} files") if f else ("file", "files"))[1]


def noun(p: Path, n: int) -> str:
    f = "pdf" if ext(p) == "pdf" else family(p)
    one, many = NOUNS.get(f, (f".{f} file", f".{f} files") if f else ("file", "files"))
    return one if n == 1 else many


def count(paths: list[Path], photos: bool = False) -> str:
    """'4 PDFs', '101 photos', '3 photos and videos', '3 files': what the files are, counted."""
    n = len(paths)
    fams = {"pdf" if ext(p) == "pdf" else family(p) for p in paths}
    if photos:
        return f"{n} " + {"image": "photo" if n == 1 else "photos", "video": "video" if n == 1 else "videos"}.get(
            fams.pop() if len(fams) == 1 else "", "photos and videos")
    return f"{n} {noun(paths[0], n) if len(fams) == 1 else 'file' if n == 1 else 'files'}"


def short(folder: str | Path) -> str:
    """A folder as you would say it: 'Downloads/docs' for ~/Downloads/docs."""
    p = Path(folder)
    return str(p.relative_to(config.HOME)) if p.is_relative_to(config.HOME) else str(p)


def day(when: datetime) -> str:
    return f"{when:%-d %b %Y}"


def pieces(stem: str) -> list[tuple[str, list[str]]]:
    """A name's words in order, each as (word, its camelCase parts), lowercase, in any script.
    Trailing numbers, plain numbers, hashes and camera or app defaults drop out: 'BoxLab02' ->
    ('boxlab', ['box', 'lab']), 'str1' -> ('str', ['str'])."""
    s = re.sub(r"[\W_]+", " ", stem)
    s = re.sub(rf"\b(?:{DEFAULT_NAME})\b", " ", s, flags=re.I)
    out = []
    for t in s.split():
        t = re.sub(r"(?<=[^\W\d_])\d+$", "", t)
        if t.isdigit() or HEX.fullmatch(t.lower()) or re.fullmatch(DEFAULT_NAME, t, flags=re.I):
            continue
        parts = re.findall(r"[A-Z]{2,}(?![a-z])|[A-Z]?[a-z]+", t) if CAMEL.fullmatch(t) else [t]
        out.append((t.lower(), [x.lower() for x in parts]))
    return out


def tokens(p: Path) -> list[str]:
    """Every word of the name in order, small ones included: what a folder name is cut from."""
    return [x for _, parts in pieces(p.stem) for x in parts]


def words_in(stem: str) -> list[str]:
    out = []
    for whole, parts in pieces(stem):
        out += [whole] + parts * (len(parts) > 1)
    return list(dict.fromkeys(w for w in out if (len(w) >= 3 or w in SHORT) and w not in FILLER))


def words(p: Path) -> list[str]:
    """Topic words of a name: 'feliks_invoice_3.pdf' -> ['feliks', 'invoice'], 'SergeyKirakosyanCV'
    -> ['sergeykirakosyancv', 'sergey', 'kirakosyan', 'cv']."""
    return words_in(p.stem)


def phrase(w: str, members: list[Path]) -> str:
    """A new folder's name: the longest run of name words around w that every member shares, with
    filler cut off both ends. 'mahou_tsukai_no_yoru' gives 'mahou-tsukai-no-yoru', not 'mahou'."""
    seqs = [tokens(p) for p in members]
    first = seqs[0]
    if w not in first:
        return w

    def shared(a: int, b: int) -> bool:
        run = first[a:b]
        return all(any(s[k:k + len(run)] == run for k in range(len(s))) for s in seqs[1:])
    a = first.index(w)
    b = a + 1
    while a > 0 and shared(a - 1, b):
        a -= 1
    while b < len(first) and shared(a, b + 1):
        b += 1
    run = first[a:b]
    weak = lambda x: x != w and (x in FILLER or (len(x) < 3 and x not in SHORT))  # noqa: E731
    while weak(run[0]):
        run.pop(0)
    while weak(run[-1]):
        run.pop()
    return "-".join(run)


def base(p: Path) -> str:
    """The name without a copy number: 'transcript (3).md' and 'transcript.md' are one name."""
    return re.sub(r"\s*\(\d+\)$", "", p.stem.lower())


def version(p: Path) -> str | None:
    """The name its versions share, copy numbers and '-1' suffixes off: 'CV-1.pdf' and 'CV (2).pdf'
    -> 'cv'. None for a generic name: 'document(1).pdf' and 'document(3).pdf' are two documents."""
    if generic(p):
        return None
    k = re.sub(r"[-_ ]+\d{1,2}$", "", base(p))
    return k if len(k) >= 4 else None


def made_by(p: Path) -> str | None:
    """The app default a name starts with, the way a folder for it is named: 'Screen Shot 2022-09-29'
    and 'Screenshot_2025' -> 'screenshot'."""
    m = re.match(rf"(?:{DEFAULT_NAME})\b", re.sub(r"[^a-z0-9]+", " ", p.stem.lower()).strip())
    return re.sub(r" from$", "", m.group()).replace(" ", "").removesuffix("s") if m else None


def zoned(folder: str | Path) -> Path | None:
    """The drop zone a path lies in (the deepest), or None."""
    hits = [z for z in config.DROP_ZONES if Path(folder).is_relative_to(z)]
    return max(hits, key=lambda z: len(z.parts)) if hits else None


def root(p: str | Path) -> Path | None:
    return next((r for r in config.ROOTS if Path(p).is_relative_to(r)), None)


def depth(folder: str | Path) -> int | None:
    """How far below its root a folder sits (the root itself is 0), None outside the roots."""
    r = root(folder)
    return None if r is None else len(Path(folder).relative_to(r).parts)


def settled(p: Path) -> bool:
    """In a folder you made: below a root, and not loose in a drop zone or a Downloads pile."""
    return bool(depth(p.parent)) and p.parent not in config.DROP_ZONES and p.parent not in config.PILES


def taken(p: Path) -> tuple[datetime, str] | None:
    """When a camera took this photo or video and how we know: EXIF DateTimeOriginal with a camera
    maker, else a date stamp name (videos by name only). Pillow cannot open HEIC, but its EXIF
    block reads fine on its own."""
    exif = None
    if family(p) == "image":
        try:
            with Image.open(p) as im:
                exif = im.getexif()
        except Exception:
            try:
                with p.open("rb") as f:
                    head = f.read(HEAD)
                if m := TIFF.search(head):
                    exif = Image.Exif()
                    exif.load(head[m.start():])
            except Exception:
                exif = None
    if exif and exif.get(0x010F):  # Make: a camera wrote this
        try:
            return datetime.strptime(str(exif.get_ifd(0x8769).get(36867)), "%Y:%m:%d %H:%M:%S"), "photo"
        except ValueError:
            pass
    if m := CAMERA.match(p.stem.lower()):
        try:
            return datetime(*map(int, m.groups())), "name"
        except ValueError:
            pass
    return None


def booru(p: Path) -> bool:
    return family(p) == "image" and bool(BOORU.fullmatch(base(p)))


@lru_cache(maxsize=4096)
def _pdf(path: str, mtime: float) -> tuple[int, str]:
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0, ""
    info = dict(line.split(":", 1) for line in out.splitlines() if ":" in line)
    pages = info.get("Pages", "0").strip()
    return int(pages) if pages.isdigit() else 0, info.get("Title", "").strip()


def pdf(p: Path) -> tuple[int, str]:
    """(pages, title) of a PDF; (0, '') for anything else."""
    return _pdf(str(p), p.stat().st_mtime) if ext(p) == "pdf" else (0, "")


def evidence(p: Path) -> set[str]:
    """The words a document's name and PDF title say about it ('HaykKyutneryan.pdf' is titled
    'CV_with projects'); empty for anything else, and for a document too long to be a paper."""
    if family(p) != "doc":
        return set()
    pages, title = pdf(p)
    return set() if pages > LONG else set(words(p)) | set(words_in(title))


@lru_cache(maxsize=4096)
def _front(path: str, mtime: float) -> str:
    try:
        return subprocess.run(["pdftotext", "-l", str(FRONT), "-q", path, "-"],
                              capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def front(p: Path) -> set[str]:
    """The words on a PDF's first pages; empty for a scan (no text in it) and for anything else."""
    return set(words_in(_front(str(p), p.stat().st_mtime))) if ext(p) == "pdf" else set()


def paper(p: Path) -> tuple[str, bool, bool] | None:
    """(note, strong, ticked) when this document is a paper you keep, else None. A paper word in its
    name or title is strong: the file goes to papers whatever routing says, ticked when your name
    (anywhere on its first pages) or a personal word is on it too. On the first pages alone, only a
    passport, a visa or a bank statement beside your name makes a paper (see SAID).
    Your name alone is not strong: your invoices carry it as well, so it only decides when routing
    finds no sure home. Coursework and documents of over LONG pages are no papers."""
    if family(p) != "doc":
        return None
    pages, title = pdf(p)
    named = set(words(p)) | set(words_in(title))
    if pages > LONG or named & ACADEMIC:
        return None
    text = front(p)
    mine = bool((named | text) & config.OWNER)
    if hit := sorted(named & PAPER):
        if mine or named & PERSONAL:
            return f"a paper: named {hit[0]}", True, True
        if ext(p) != "pdf":
            return f"named {hit[0]}, but your name is not in its name (its text was not read)", True, False
        if not text:
            return f"named {hit[0]}, but it is a scan: ariadne could not read whether your name is on it", True, False
        return f"named {hit[0]}, but your name is not on its first pages", True, False
    if text & config.OWNER and (hit := sorted(text & SAID)) and not text & ACADEMIC:
        return f"a paper: your name and '{hit[0]}' are on its first pages", True, True
    return ("a paper: your name is on it", False, True) if named & config.OWNER else None


class Shelf:
    """How you already file things, read off the files you filed: folders by name (none in a drop
    zone), and how many files of each type sit under and directly in each folder."""

    def __init__(self, filed: list[str]):
        self.filed: set[str] = set()
        self.named: dict[str, set[str]] = defaultdict(set)
        self.held: Counter = Counter()  # (folder, type) -> files under it
        self.under: Counter = Counter()  # folder -> files under it, its own included
        self.total: Counter = Counter()  # type -> filed files, drop zone folders included
        self.direct: Counter = Counter()  # (folder, type family) -> files directly in it
        self.inside: dict[str, Counter] = defaultdict(Counter)  # folder -> type family -> files directly in it
        self.add(filed)

    def add(self, filed: list[str]) -> None:
        for p in filed:
            self.filed.add(p)
            e, f = ext(Path(p)), family(Path(p))
            self.total[e] += 1
            self.direct[str(Path(p).parent), f] += 1
            self.inside[str(Path(p).parent)][f] += 1
            if zoned(p):
                continue
            for a in engine.ancestors(str(Path(p).parent)):
                self.named[Path(a).name.lower()].add(a)
                self.held[a, e] += 1
                self.under[a] += 1

    def plus(self, filed: list[str]) -> "Shelf":
        """This shelf with these files filed too: the shelf as it will be once a plan is moved."""
        s = Shelf([])
        s.filed, s.held, s.under, s.total, s.direct = set(self.filed), self.held.copy(), self.under.copy(), \
            self.total.copy(), self.direct.copy()
        s.named = defaultdict(set, {k: set(v) for k, v in self.named.items()})
        s.inside = defaultdict(Counter, {k: v.copy() for k, v in self.inside.items()})
        s.add(filed)
        return s

    def proposable(self, folder: str, p: Path, named: bool = False) -> bool:
        """A folder ariadne may offer p by routing: outside Downloads, no saved page's '_files'
        folder, no vault (a folder holding .obsidian, or one whose files mostly sit in folders inside
        it), and holding files of p's type directly ('client_secret.json' is not for a 3D print
        project of .py and .3mf files). A folder `named` for the file (its screenshots) may keep most
        of its files in folders inside it: Pictures/screenshots holds 'One Pace'."""
        f = Path(folder)
        if zoned(f) or f.name.endswith("_files") or (f / ".obsidian").is_dir():
            return False
        own = sum(self.inside[folder].values())
        return (self.direct[folder, family(p)] > 0 and (named or own * 2 >= self.under[folder])
                and not self.foreign(folder, p))

    def holding(self, names: list[str], p: Path) -> str | None:
        """The folder named one of these that holds files of p's type family directly, where p
        would land ('artoria' art is not for blender/Artoria, whose pictures sit in subfolders).
        When several share the name, the one holding most."""
        f = family(p)
        hits = [d for n in names if n not in PLAIN for d in self.named.get(n, ()) if self.proposable(d, p, named=True)]
        return max(hits, key=lambda d: (self.direct[d, f], -len(Path(d).parts))) if hits else None

    def named_like(self, p: Path) -> tuple[str, str] | None:
        """(folder, word): a folder named like a word of the file's name. Never for a generic name."""
        if generic(p):
            return None
        for w in words(p):
            if folder := self.holding([w], p):
                return folder, w
        return None

    def made_for(self, p: Path) -> tuple[str, str] | None:
        """(folder, word): the folder you keep for an app default, 'Screenshot_2025.jpg' -> screenshots."""
        w = made_by(p)
        return (folder, w) if w and (folder := self.holding([w, w + "s"], p)) else None

    def home(self, p: Path) -> str | None:
        """The one folder holding every filed file of this type, when there are enough of them."""
        e = ext(p)
        if not e or self.total[e] < TYPE_HOME:
            return None
        homes = [f for (f, t), n in self.held.items() if t == e and n == self.total[e]]
        return max(homes, key=lambda f: len(Path(f).parts)) if homes else None

    def lone(self, p: Path) -> bool:
        """Nothing else of its type family sits in its folder."""
        return self.direct[str(p.parent), family(p)] - (str(p) in self.filed) <= 0

    def foreign(self, folder: str, p: Path) -> str | None:
        """'videos' when the folder is sorted by a type and it is not p's: no .md transcript goes
        into a folder of audio files."""
        c = self.inside.get(folder)
        if not c or sum(c.values()) < GROUP:
            return None
        f, n = c.most_common(1)[0]
        return plural(f) if f != family(p) and n / sum(c.values()) >= decide.TYPED else None

    def alike(self, p: Path, folder: str) -> str:
        """'like the 12 PDFs in docs': what a routed file joins."""
        n = self.direct[folder, family(p)]
        return f"like the {n} {noun(p, n)} in {Path(folder).name}" if n else f"like the files in {Path(folder).name}"


def expand(paths: list[str], kept: set[str]) -> tuple[list[Path], list[dict]]:
    """The files to plan for (a folder means the files loose in it), and the ones left out, with why."""
    out, left, seen = [], [], set()
    for x in paths:
        p = Path(x)
        for f in sorted(c for c in p.iterdir() if not c.is_dir()) if p.is_dir() else [p]:
            if f in seen or f.name.startswith("."):
                continue
            seen.add(f)
            if not f.is_file():
                why = "not a file"
            elif not f.stat().st_size:
                why = "empty file"
            elif not wanted(f) or (root(f) and not watch.indexable(f)):
                why = UNREAD
            elif str(f) in kept:
                why = "marked Keep in the Inbox"
            else:
                out.append(f)
                continue
            left.append({"path": str(f), "why": why})
    return out, left


def unfit(d: dict) -> str | None:
    """Why the file's own decision is no place to put it, or None."""
    if why := decide.groundless(d):
        return why
    if d.get("fits", 1.0) < decide.NONE_FITS:
        return "no folder fits it well"
    if not Path(d["folder"]).is_dir() and not d.get("planned"):  # a folder the plan makes is no folder gone
        return "its folder is gone"
    return None


def sure(d: dict) -> bool:
    """Over the bar ariadne auto-moves at, on the share alone."""
    return d["share"] >= (decide.AUTO_JEV if d["by"] == "jev" else decide.VOTE_SURE)


def doubt(d: dict) -> str | None:
    """Why a routed file is not sure enough to pre-tick, in plain words, or None when it is. These
    are decide.confident's own bars, so a sure file here is one ariadne would also move on its own."""
    share = f"{d['share']:.0%}"
    if not sure(d):
        return f"guess, {share}"
    if d["by"] == "jev" and d.get("fits", 1.0) < decide.AUTO_FITS:
        return f"{share} the closest folder, but not a close fit"
    if d.get("named") and d.get("top", 1.0) < decide.NAME_FLOOR:
        return f"{share}, but only by a loosely similar name"
    return None


def stray(p: Path, d: dict, shelf: Shelf) -> bool:
    """A filed file that clearly sits in the wrong place: it has content, nothing of its type is
    beside it, and the vote (not Jev, whose answers vary) is sure of another folder. 'spec.json' in a
    3D print project has no content to judge, so it stays however sure the vote is of its name."""
    return (kind(p) != "name" and d["by"] == "vote" and decide.confident(d)
            and d["folder"] != str(p.parent) and shelf.proposable(d["folder"], p))


def same(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def first(p: Path) -> tuple:
    """Which of two copies stays in the plan: the shorter name ('deck.ydk' over 'deck (2).ydk')."""
    return len(p.name), p.name


@dataclass
class Ctx:
    shelf: Shelf
    where: Callable[[Path], dict]  # the file's routing decision
    copies: dict[Path, str | None]  # a copy of it elsewhere in the index, or None
    picked: set[Path]
    ghosts: frozenset[Path] = frozenset()  # siblings that only count toward a group: they get no row


def row(folder: str | Path, why: str, rule: str, note: str, ticked: bool = True, share=None, **more) -> dict:
    return {"folder": str(folder), "why": why, "kind": rule, "note": note, "ticked": ticked, "share": share, **more}


def copied(p: Path, c: Ctx) -> dict | None:
    """A copy you filed stays put; of two picked copies the first stays in the plan; a copy loose in
    another pile is left for that one to be organized."""
    if not (dup := c.copies.get(p)):
        return None
    d = Path(dup)
    if d in c.picked:
        return {"left": f"a copy of {d.name} in this selection"} if first(d) < first(p) else None
    if settled(d):
        return {"left": f"a copy is already in {short(d.parent)}" + (f" as {d.name}" if d.name != p.name else "")}
    return {"left": f"a copy of it is loose in {short(d.parent)} as {d.name}; organize that one"}


def camera(p: Path, c: Ctx) -> dict | None:
    if family(p) not in ("image", "video") or not (t := taken(p)):
        return None
    when, how = t
    what = "video" if family(p) == "video" else "photo"
    return row(PHOTOS / f"{when:%Y}", f"photos from {when:%Y}", "photo",
               f"{what} taken {day(when)}" + (", by its name" if how == "name" else ""), when=when)


def recording(p: Path, c: Ctx) -> dict | None:
    if family(p) != "video" or not (made_by(p) == "screenrecording" or SCREEN.match(p.stem.lower())):
        return None
    folder = c.shelf.holding(["screen-recordings", "screenrecordings", "recordings"], p) or RECORDINGS
    return row(folder, "screen recordings", "name", "a screen recording")


def saved_art(p: Path, c: Ctx) -> dict | None:
    if family(p) != "image":
        return None
    for site, pattern in (("pixiv", PIXIV), ("twitter", TWITTER)):
        if pattern.fullmatch(p.stem):
            return row(ART / site, f"art saved from {site}", "art", f"saved from {site}")
    return None


def cv(p: Path, c: Ctx) -> dict | None:
    """A CV, by its name or its PDF title: yours to papers/cv, anyone else's to papers/cv/others."""
    ws = evidence(p)
    if not ws & CV:
        return None
    if ws & config.OWNER:
        return row(PAPERS / "cv", "your CVs", "paper", "your CV")
    return row(PAPERS / "cv/others", "other people's CVs", "paper", "someone else's CV")


def by_name(p: Path, c: Ctx) -> dict | None:
    if hit := c.shelf.named_like(p):
        return row(hit[0], "named like this folder", "name", f"named {hit[1]}")
    if hit := c.shelf.made_for(p):
        return row(hit[0], f"your {Path(hit[0]).name}", "name", f"a {hit[1]}")
    return None


def by_type(p: Path, c: Ctx) -> dict | None:
    if not generic(p) and (home := c.shelf.home(p)):
        return row(home, f"where your .{ext(p)} files are", "type", f"like your other .{ext(p)} files", ext=ext(p))
    return None


RULES = [copied, camera, recording, saved_art, cv, by_name, by_type]  # first hit wins; none asks for routing


def needs(paths: list[Path], shelf: Shelf, copies: dict[Path, str | None]) -> list[Path]:
    """The files plan() will ask `where` about, so the daemon can route them ahead, in parallel:
    lone filed files (is it a stray?) and loose files no rule settles."""
    c = Ctx(shelf, lambda p: {}, copies, set(paths))
    return [p for p in paths if settled(p) and shelf.lone(p) and kind(p) != "name"] + \
        [p for p in paths if not settled(p) and not booru(p) and not (paper(p) or ("", False))[1]
         and not any(r(p, c) for r in RULES)]


def counts(p: Path, d: dict, shelf: Shelf) -> bool:
    """A decision a group may follow: a real guess, into a folder ariadne may offer (none in
    Downloads: a topic is no download type), and for a picture a close sibling."""
    return (not unfit(d) and d["share"] >= GUESS and shelf.proposable(d["folder"], p)
            and not (kind(p) == "image" and d.get("top", 1.0) < NEAR))


def parent(members: list[Path], ds: list[dict], shelf: Shelf, pictures: bool = False) -> Path:
    """Where a new folder for a group goes: the deepest folder above all the members' own picks, at
    most PARENT_DEPTH below a root and outside the drop zones. Else the root the members already
    sit in, else Documents/papers for documents and Documents for the rest. A group of nothing but
    pictures (`pictures`) goes under Pictures: BoxLab photos are no project in Documents."""
    picks = [d["folder"] for p, d in zip(members, ds) if counts(p, d, shelf)]
    common = Path(os.path.commonpath(picks)) if picks else None
    if common and depth(common) is not None:
        while depth(common) > PARENT_DEPTH:
            common = common.parent
        if not pictures or common.is_relative_to(PICTURES):
            return common
    if pictures:
        return PICTURES
    roots = {root(p) for p in members}
    r = roots.pop() if len(roots) == 1 else None
    if r and not zoned(r):
        return r
    return PAPERS if all(family(p) == "doc" for p in members) else FALLBACK


def destination(w: str, name: str, members: list[Path], ds: list[dict], shelf: Shelf,
                pictures: bool) -> tuple[str, str] | None:
    """(folder, how) for a group: 'votes' when most members' decisions agree on a folder of yours,
    'named' when a folder of that name is already where the new one would go, else 'new'. A paper
    word ('id', 'visa') always goes to Documents/papers/<word>. None when the named folder is one
    ariadne may not look in (an app's own folder)."""
    if w in PAPER:
        under = PAPERS
    else:
        votes = Counter(d["folder"] for p, d in zip(members, ds) if counts(p, d, shelf))
        if votes:
            folder, n = votes.most_common(1)[0]
            if n > len(members) / 2:
                return folder, "votes"
        under = parent(members, ds, shelf, pictures)
    hit = [c for c in under.iterdir() if c.is_dir() and c.name.lower() in (name, w)] if under.is_dir() else []
    if hit:
        return (str(hit[0]), "named") if watch.visible(hit[0]) else None
    return str(under / name), "new"


def grouped(pool: dict[Path, list[str]], spare: dict[Path, list[str]], c: Ctx, put) -> None:
    """Files sharing a name word, biggest group first, take them out of the pool. A word must span
    GROUP different names of one type family ('Adobe Scan' PDFs and Adobe installer torrents are two
    families, no group), or of any type for a paper word (ID.pdf and two ID photos). Then files of other types with the word join (a project's .zip and its key). Your
    own name groups nothing, and a word under TOPIC letters makes no new folder. Saved art makes
    topic folders under Pictures/art. Ticks: a new folder, or one named the word, ticks every member,
    since the shared word over 3 names is the evidence. Joining a folder by majority ticks only the
    members sure of it on their own."""
    tried: set[tuple[str, str]] = set()
    while True:
        by = defaultdict(list)
        for p, ws in pool.items():
            for w in ws:
                if w in config.OWNER:
                    continue
                for key in ((w, family(p)), (w, "*")):
                    if key not in tried:
                        by[key].append(p)
        groups = [(k, ps) for k, ps in by.items() if len({base(p) for p in ps}) >= GROUP and (k[1] != "*" or k[0] in PAPER)]
        if not groups:
            return
        # ties: the word that comes first in the names, then the alphabet
        key, members = min(groups, key=lambda g: (-len(g[1]), sum(pool[p].index(g[0][0]) for p in g[1]), g[0]))
        tried.add(key)
        w = key[0]
        name = phrase(w, members)
        if all(booru(p) for p in members):
            folder = ART / name if len(members) >= ART_TOPIC else ART
            for p in members:
                put(p, row(folder, f"art saved from a booru, named {name}" if folder != ART else "art saved from a booru",
                           "art", f"saved art, named {w}" if folder != ART else "saved art"))
                del pool[p]
            continue
        joins = [p for p in pool if w in pool[p] and p not in members and not booru(p)]
        joins += [p for p in spare if w in spare[p]]
        ds = [{} if w in PAPER else c.where(p) for p in members]
        pictures = all(family(p) == "image" for p in members + joins)
        if not (dest := destination(w, name, members, ds, c.shelf, pictures)):
            continue
        folder, how = dest
        if how == "new" and len(w) < TOPIC and w not in SHORT:
            continue
        beside = sum(p in c.ghosts for p in members + joins)  # siblings you did not pick
        also = f", like {files(beside)} beside it" if beside else ""
        for p, d in zip(members, ds):
            mine = how != "votes" or (d["folder"] == folder and not doubt(d))
            note = f"named {w}{also}" if mine else f"named {w}, {doubt(d)}" if d["folder"] == folder else \
                f"named {w}, but on its own it looks like {short(d['folder'])}"
            if w in PAPER and family(p) == "doc":  # a policy paper named visa joins papers/visa unticked
                kept = paper(p)
                mine = bool(kept and kept[2])
                note = f"named {w}" if mine else kept[0] if kept else f"named {w}, but it reads as coursework or a book"
            put(p, row(folder, f"{{}} named {w}", "word", note, ticked=mine,
                       share=d["share"] if d.get("folder") == folder else None, word=w))
            del pool[p]
        for p in joins:
            put(p, row(folder, f"{{}} named {w}", "word", f"named {w}, like the others", ticked=how != "votes", word=w))
            pool.pop(p, None)
            spare.pop(p, None)


def size_class(p: Path) -> int | None:
    """Pictures within a fifth of a size of each other share a class: sprites of 125 to 151 px, or
    pages of 800 px."""
    try:
        with Image.open(p) as im:
            return round(math.log2(max(min(im.size), 1)) * 2)
    except Exception:
        return None


def sets(pool: dict[Path, list[str]], spare: dict[Path, list[str]], c: Ctx, put) -> None:
    """Pictures saved the same day at the same size, 3 different names or more, are one set: a
    game's sprites, a comic's pages. A file of another type from that day whose word starts like
    theirs joins and names the set ('bebeheist.clip' with 'bebestunned.png'); else the set's longest
    word, else it has none. The set goes where most of them point, else to a new folder named for
    it (under Pictures when it is only pictures, with a project file under Documents), else (no
    word) with your saved art. Never pre-ticked: a day and a size are a hint, not a name."""
    when = lambda p: datetime.fromtimestamp(p.stat().st_mtime).date()  # noqa: E731
    by = defaultdict(list)
    for p in pool:
        if family(p) == "image" and not booru(p) and (k := size_class(p)) is not None:
            by[p.parent, when(p), k].append(p)
    for (folder, d0, _), members in by.items():
        if len({base(p) for p in members}) < GROUP:
            continue
        stems = {w[:4] for p in members for w in pool[p] if len(w) >= TOPIC}
        others = {**pool, **spare}
        joins = [q for q in others if q not in members and q.parent == folder and family(q) != "image"
                 and when(q) == d0 and any(w[:4] in stems for w in others[q] if len(w) >= TOPIC)]
        named = [w for q in joins for w in others[q] if len(w) >= TOPIC]
        if not named and sum(any(len(w) >= TOPIC for w in pool[p]) for p in members) >= 2:
            named = [w for p in members for w in pool[p] if len(w) >= TOPIC]  # one odd name ('eifvja') names nothing
        name = max(named, key=len, default=None)
        ds = [c.where(p) for p in members]
        votes = Counter(d["folder"] for p, d in zip(members, ds) if counts(p, d, c.shelf))
        top = votes.most_common(1)[0] if votes else (None, 0)
        if top[1] > len(members) / 2:
            dest = top[0]
        elif name:
            dest = str(parent(members, ds, c.shelf, pictures=not joins) / name)
        else:
            dest = str(ART)
        n, saved = len(members), f"{d0:%-d %b %Y}"
        for p in members:
            put(p, row(dest, f"{{}} saved {saved} at one size", "set", f"one of {n} pictures saved {saved} at one size",
                       ticked=False, word=name))
            del pool[p]
        for q in joins:
            put(q, row(dest, f"{{}} saved {saved} at one size", "set", f"made the same day as the {n} pictures",
                       ticked=False, word=name))
            pool.pop(q, None)
            spare.pop(q, None)


def routed(p: Path, c: Ctx) -> dict:
    """A one-off's row from its own decision, or {"left": why}."""
    if booru(p):
        return row(ART, "art saved from a booru", "art", "saved art")
    kept = paper(p)
    if kept and kept[1]:
        return row(PAPERS, "papers to keep", "paper", kept[0], ticked=kept[2])
    d = c.where(p)
    if kept and not (counts(p, d, c.shelf) and not doubt(d)):  # your name, and no sure home of its own
        return row(PAPERS, "papers to keep", "paper", kept[0])
    return placed(p, d, c.shelf)


def placed(p: Path, d: dict, shelf: Shelf) -> dict:
    """The row a routing decision gives p, or {"left": why}. Never a folder in Downloads, a vault,
    a saved page's files or a folder without p's type in it: those are no place you would pick."""
    if why := unfit(d):
        return {"left": why}
    folder = d["folder"]
    if z := zoned(folder):  # its only match is in Downloads: the home for its type instead
        if p.is_relative_to(z) or not (home := HOMES.get(family(p))) or (family(p) == "doc" and pdf(p)[0] > LONG):
            return {"left": "not sure where this goes"}
        return row(home, f"{{}} like downloads in {Path(folder).name}", "home",
                   f"closest is {short(folder)}, in Downloads", ticked=False)
    if holds := shelf.foreign(folder, p):
        return {"left": f"the closest folder, {Path(folder).name}, keeps {holds}, not {plural(family(p))}"}
    if not shelf.proposable(folder, p):
        return {"left": "not sure where this goes"}
    if kind(p) == "image" and d.get("top", 1.0) < NEAR:
        return {"left": "nothing you filed looks much like it"}
    if d["share"] < GUESS:
        return {"left": "not sure where this goes"}
    unsure = doubt(d)
    note = unsure or f"{shelf.alike(p, folder)}, {d['share']:.0%} sure"
    return row(folder, "like the files already there", "routed", note, ticked=not unsure, share=d["share"])


def twins(paths: list[Path]) -> dict[Path, Path]:
    """Picked files with the same bytes as another picked file -> the one that stays in the plan."""
    by = defaultdict(list)
    for p in paths:
        by[p.stat().st_size].append(p)
    out = {}
    for ps in by.values():
        ps.sort(key=first)
        for i, p in enumerate(ps):
            if p not in out and (keep := next((q for q in ps[:i] if q not in out and same(p, q)), None)):
                out[p] = keep
    return out


def kin(rows: dict[str, dict], left: list[dict]) -> None:
    """Versions of one name share the fate of the one ariadne is surest of: one folder, one tick.
    A version left only for want of a guess follows too."""
    paths = [Path(r) for r in rows] + [Path(x["path"]) for x in left if x["why"] in UNSURE]
    keys = sorted({k for p in paths if (k := version(p))}, key=len)
    head = {}
    for k in keys:  # a longer name that starts with a shorter one is its variant: '23-w' and '23-wՖԱմ51'
        head[k] = next((h for h in keys if len(h) >= 8 and len(h) < len(k) and k.startswith(h)), k)
    fam = defaultdict(list)
    for p in paths:
        if k := version(p):
            fam[head[k]].append(p)
    for ps in fam.values():
        ranked = [p for p in ps if str(p) in rows]
        if len(ps) < 2 or not ranked:
            continue
        lead = max(ranked, key=lambda p: (rows[str(p)]["share"] or 1.0, rows[str(p)]["ticked"]))
        r = rows[str(lead)]
        for p in ps:
            if p != lead:
                rows[str(p)] = {**r, "path": str(p), "note": f"a version of {lead.name}"}
        left[:] = [x for x in left if Path(x["path"]) not in ps]


def summary(g: dict, rows: list[dict]) -> str:
    """The group's headline: one plain sentence saying what Move does."""
    kinds = Counter(r["kind"] for r in rows)
    k = kinds.most_common(1)[0][0]
    ticked = [Path(r["path"]) for r in rows if r["ticked"]]
    word = rows[0].get("word")
    what = count(ticked or [Path(r["path"]) for r in rows], photos=k == "photo")
    if k == "photo":
        days = sorted(r["when"] for r in rows if r["ticked"]) or sorted(r["when"] for r in rows)
        what += f" taken {day(days[0])}" if day(days[0]) == day(days[-1]) else \
            f" taken between {day(days[0])} and {day(days[-1])}"
    elif k == "word":
        what += f" named {word}"
    elif k == "set":
        what += " saved the same day at one size"
    where = short(g["folder"])
    if g["new"]:
        line = f"Make {where} and move {what} into it" if ticked else f"Nothing ticked: {what} could go into a new {where}"
    elif not any(Path(g["folder"]).iterdir()):  # a folder you made for this, still empty
        line = f"Move {what} into {where}" if ticked else f"Nothing ticked: {what} could go into {where}"
    else:
        one = len(ticked or rows) == 1
        tail = {"name": "the folder its name points to" if one else "the folder their names point to",
                "type": f"where all your .{rows[0].get('ext')} files are",
                "word": "where most of them fit", "set": "where most of them fit", "photo": "with your other photos",
                "art": "with your other saved art", "paper": "with your other papers",
                }.get(k, "where files like it are" if one else "where files like them are")
        line = f"Move {what} into {where}, {tail}" if ticked else f"Nothing ticked: {what} could go into {where}, {tail}"
    rest = len(rows) - len(ticked)
    if ticked and rest:
        line += f"; {rest} more unticked"
    return line


def named_as(proposed: str, renamed: dict[str, str]) -> str:
    """A proposed folder under the name you gave it, or a folder inside one you renamed: with papers
    renamed to paperwork, papers/cv is paperwork/cv. A folder that exists keeps its own name."""
    if Path(proposed).exists():
        return proposed
    hits = [k for k in renamed if proposed == k or proposed.startswith(k + "/")]
    k = max(hits, key=len, default=None)
    return renamed[k] + proposed[len(k):] if k else proposed


def siblings(paths: list[Path], kept: set[str]) -> list[Path]:
    """For a small selection, the unpicked files loose beside a picked loose file that share a topic
    word with it: 'varpet_avatar.png' alone is one of six varpet files in Downloads, and groups as
    one. They count toward a group and get no row."""
    if len(paths) > SMALL:
        return []
    picked, out = set(paths), []
    for p in paths:
        ws = set(words(p)) - config.OWNER
        if settled(p) or not ws:
            continue
        near, _ = expand([str(p.parent)], kept)
        out += [q for q in near if q not in picked and q not in out and ws & set(words(q))]
    return out


Again = Callable[[list[Path], dict[str, str], set[str]], dict[Path, dict | None]]


def fixed(rows: dict[str, dict], left: list[dict], open_: set[Path], shelf: Shelf, again: Again) -> None:
    """Route the files no folder took again, as if the plan's ticked rows were already moved: after
    a Move, a second plan sent 6 bank statements and visa forms to the papers the first one filled.
    `again(paths, planned, filling)` decides each file with every planned file (path -> folder) in
    its planned folder; `filling` are the folders a file may join, so Jev is asked only about those. A file joins only a folder the plan fills, and only on a decision sure enough to tick;
    an unticked row whose decision now points to such a folder is asked about that one instead.
    Repeats until no new file is ticked, ROUNDS times at most."""
    for _ in range(ROUNDS):
        planned = {path: r["folder"] for path, r in rows.items() if r["ticked"]}
        if not planned or not open_:
            return
        moved = shelf.plus([str(Path(f) / Path(x).name) for x, f in planned.items()])
        # a year of photos or a site's saved art says when or where from, not what: no look-alike joins it
        filling = {r["folder"] for r in rows.values() if r["ticked"] and r["kind"] not in ("photo", "art")}
        found = []
        ds = again(sorted(open_), planned, filling)
        for p in sorted(open_):
            if not (d := ds.get(p)) or d["folder"] not in filling:
                continue
            r = placed(p, {**d, "planned": True}, moved)
            if "left" in r:
                continue
            if r["ticked"]:
                rows[str(p)] = {"path": str(p), **r}
                found.append(p)
            elif str(p) in rows:  # still unsure, but asked about the folder it now points to
                rows[str(p)] = {"path": str(p), **r}
        if not found:
            return
        open_ -= set(found)
        gone = {str(p) for p in found}
        left[:] = [x for x in left if x["path"] not in gone]


def plan(paths: list[Path], shelf: Shelf, where: Callable[[Path], dict], copies: dict[Path, str | None],
         rejected: dict[str, dict[str, float]], renamed: dict[str, str], left: list[dict],
         ghosts: list[Path] = (), again: Again | None = None) -> dict:
    """The plan for these files. `where(path)` is the file's routing decision; `copies` the copy of
    each file elsewhere in the index. A proposed folder you renamed before takes your name.
    `ghosts` are siblings that count toward a group (see siblings); `again` routes the leftovers
    once more against the folders the plan fills (see fixed)."""
    c = Ctx(shelf, where, copies, set(paths), frozenset(ghosts))
    rows: dict[str, dict] = {}  # file -> its row, with what the group headline needs

    def put(p: Path, r: dict) -> None:
        if p in c.ghosts:
            return
        if "left" in r:
            left.append({"path": str(p), "why": r["left"]})
        else:
            rows[str(p)] = {"path": str(p), **r}

    loose = []
    for p in paths:
        if settled(p) and not (shelf.lone(p) and stray(p, where(p), shelf)):
            put(p, {"left": f"already filed in {short(p.parent)}"})
        else:
            loose.append(p)
    dupes = twins(loose)
    pool: dict[Path, list[str]] = {}
    for p in loose:
        if p in dupes:
            put(p, {"left": f"a copy of {dupes[p].name} in this selection"})
        elif r := next((r for rule in RULES if (r := rule(p, c))), None):
            put(p, r)
        else:
            pool[p] = words(p)
    for p in c.ghosts:  # as the full pile would have them: a ghost a rule takes is in no group
        if not any(rule(p, c) for rule in RULES):
            pool[p] = words(p)
    # files ariadne cannot read may still join a group by name (a project's key file), nothing else
    spare = {Path(x["path"]): words(Path(x["path"])) for x in left
             if x["why"] == UNREAD and Path(x["path"]).is_file() and ext(Path(x["path"])) not in PARTIAL}
    grouped(pool, spare, c, put)
    for p in c.ghosts:
        pool.pop(p, None)
    sets(pool, spare, c, put)
    left[:] = [x for x in left if x["path"] not in rows]  # the unreadable files a group or set took
    open_ = set()  # files routing found no sure folder for
    for p in list(pool):
        r = routed(p, c)
        put(p, r)
        if "left" in r or (not r["ticked"] and r["kind"] in ("routed", "home")):
            open_.add(p)
    if again:
        fixed(rows, left, open_, shelf, again)
    kin(rows, left)

    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path, r in rows.items():
        p, proposed = Path(path), r["folder"]
        folder = named_as(proposed, renamed)
        there = Path(folder) / p.name
        if p.parent.is_relative_to(folder):
            left.append({"path": path, "why": "already there"})
        elif proposed in rejected.get(path, {}):
            left.append({"path": path, "why": f"you said no to {Path(proposed).name} before"})
        elif there.exists() and same(p, there):
            left.append({"path": path, "why": f"a copy is already in {short(folder)}"})
        else:
            if there.exists():  # Move would refuse it: never ticked, and the note says why
                r.update(ticked=False, share=None, note=f"a different file named {p.name} is already there")
            out[folder, proposed].append(r)
    groups = []
    for (folder, proposed), rs in out.items():
        rs.sort(key=lambda r: Path(r["path"]).name.lower())
        why, n = Counter(r["why"] for r in rs).most_common(1)[0]
        why = why.format(files(n)) if "{}" in why else why
        g = {"folder": folder, "proposed": proposed, "new": not Path(folder).exists(), "why": why[0].upper() + why[1:]}
        g["summary"] = summary(g, rs)
        g["files"] = [{"path": r["path"], "from": str(Path(r["path"]).parent), "ticked": r["ticked"],
                       "share": r["share"], "note": r["note"]} for r in rs]
        groups.append(g)
    groups.sort(key=lambda g: (not g["new"], -len(g["files"]), g["folder"]))
    return {"groups": groups, "left": left}
