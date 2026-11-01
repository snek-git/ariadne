"""Tidy ideas for what you already filed: a file sitting in the wrong folder, a second copy of a file,
two folders holding the same thing. The rules here find candidates; Opus (judge.py) reads each one
once and only what it says yes to is shown. Both run in a background thread and are cached, so
asking never waits on them. A skipped idea stays quiet for 30 days; a muted folder gets none."""
import filecmp
import json
import os
import re
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from . import config, engine, judge
from .extract import kind
from .store import Store

SURE = 0.9  # misfiled: the vote's share for a folder in another project
ODD = 1 / 3  # misfiled: at most this share of a folder's files may point the same way
COPY = 0.99  # vector similarity that makes two files candidate copies
MERGE = 0.9  # cosine of two folders' centroids, names that are close
MERGE_SAME = 0.6  # the same, names that match
MERGES = 5  # at most this many merge ideas
CAP = 6000  # files one misfiled pass looks at
BLOCK = 512  # files voted on per matrix product
QUIET = 30 * 24 * 3600  # how long a skipped idea stays quiet
REFRESH = 600  # seconds between passes when nothing moves
SETTLE = 5  # a burst of moves lands as one pass
JUDGES = 4  # claude -p calls at once
FILED = [config.HOME / "Documents", config.HOME / "Pictures"]

_same: dict[tuple, bool] = {}
_state = threading.RLock()  # held across a read, change and write of tidy.json


def zoned(p: str) -> bool:
    """In a drop zone or a folder below one: not somewhere you filed it."""
    return any(Path(p).is_relative_to(z) for z in config.DROP_ZONES)


def same_bytes(a: str, b: str) -> bool:
    """Byte for byte the same file (not empty). Cached per pair and modification time."""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except OSError:
        return False
    if sa.st_size != sb.st_size or not sa.st_size:
        return False
    key = (a, b, sa.st_mtime, sb.st_mtime)
    if key not in _same:
        _same[key] = filecmp.cmp(a, b, shallow=False)
    return _same[key]


def copy_of(store: Store, path: str) -> str | None:
    """A copy of this file outside the drop zones: the same bytes, or for a file with content (text,
    a picture) a 0.99 look-alike. A name-only file counts by its bytes alone: its vector is its name."""
    try:
        size = os.stat(path).st_size
    except OSError:
        return None
    for p in store.sized(size):
        if p != path and not zoned(p) and same_bytes(path, p):
            return p
    v = store.file_vec(path)
    if v is None or kind(Path(path)) == "name":
        return None
    m = store.mem()
    sims = m["vecs"] @ v
    for i in np.argsort(-sims):
        if sims[i] < COPY:
            break
        p = m["paths"][i]
        if p != path and not zoned(p) and m["kinds"][i] != "name":
            return p
    return None


def apart(a: str, b: str) -> bool:
    """Neither folder is the other or holds it."""
    return a != b and not a.startswith(b + "/") and not b.startswith(a + "/")


def misfiled(m: dict) -> tuple[list[dict], list[tuple[str, str]]]:
    """Filed files under ~/Documents or ~/Pictures that the vote of their neighbors (the file itself
    left out) puts in another folder, 90% or more. Only files with content: a name-only file votes
    by its name, which sends every `plate_1.gcode` to whichever project has the most.

    Files that share a folder and a target are one change. When more than a third of a folder's
    files with content point the same way, it is about the folders, not the files: the pair goes
    back as a folder pair for the merge rules (stills beside frames is two names for two things, so
    it comes to nothing there). When fewer do, they are the odd ones out and make one card, "Move 3
    files", but only when the folder holds 3 or more files with content and the target is in
    another project: inside one project the layout is yours (a race note next to dragon monsters,
    a font's licence). Across projects the 90% is the vote for the target's whole project."""
    paths, vecs = m["paths"], m["vecs"]
    voters = np.flatnonzero(engine.labeled(paths))
    if len(voters) <= engine.K:
        return [], []
    col = {int(g): n for n, g in enumerate(voters)}
    folders = [str(Path(paths[i]).parent) for i in voters]
    pre = tuple(str(r) + "/" for r in FILED)
    files = [int(i) for i in voters if m["kinds"][i] != "name" and paths[i].startswith(pre)][:CAP]
    held = Counter(str(Path(paths[i]).parent) for i in files)
    way = defaultdict(list)  # (from, to) -> [(path, share)]
    for a in range(0, len(files), BLOCK):
        rows = files[a:a + BLOCK]
        sims = vecs[rows] @ vecs[voters].T
        sims[np.arange(len(rows)), [col[i] for i in rows]] = -1.0  # the file does not vote for itself
        top = np.argpartition(-sims, engine.K, axis=1)[:, :engine.K]
        for r, i in enumerate(rows):
            near = [folders[j] for j in top[r]]
            best = engine.vote(sims[r, top[r]], near)[0]
            here, to = str(Path(paths[i]).parent), best["folder"]
            if not to.startswith(pre) or not apart(here, to) or not Path(to).is_dir():
                continue
            share = best["share"]
            if area(here) != area(to):
                w = np.maximum(sims[r, top[r]], 0.0)
                share = float(w[[area(f) == area(to) for f in near]].sum() / (w.sum() or 1.0))
            if share >= SURE:
                way[here, to].append((paths[i], share))
    cards, pairs = [], []
    for (here, to), got in way.items():
        if len(got) > held[here] * ODD:
            pairs.append((here, to))
        elif held[here] >= 3 and area(here) != area(to):
            ps = sorted(p for p, _ in got)
            cards.append({"id": "mis:" + "|".join(ps) + f"|{to}", "kind": "misfiled",
                          "confidence": round(min(s for _, s in got), 3), "paths": ps, "from": here, "to": to})
    return cards, pairs


def area(folder: str) -> str:
    """The root and the first folder below it: `~/Documents/stl` for that folder and any inside it."""
    for r in config.ROOTS:
        if Path(folder).is_relative_to(r):
            parts = Path(folder).relative_to(r).parts
            return str(r / parts[0]) if parts else str(r)
    return folder


def project(p: str) -> str:
    """The area a file sits in."""
    return area(str(Path(p).parent))


def plain(p: str) -> str:
    """The name without the mark a second save leaves: `cv (2).pdf`, `cv-1.pdf`, `cv copy.pdf` are `cv.pdf`."""
    q = Path(p)
    return re.sub(r"( \(\d+\)|-\d+| copy( \d+)?)$", "", q.stem) + q.suffix


def keep_first(p: str) -> tuple:
    """Which copy to keep: one outside the drop zones, then one you sorted into a folder inside a
    drop zone (`~/Downloads/docs`) over one loose at its top, then the plain name, then the older."""
    try:
        t = os.stat(p).st_mtime
    except OSError:
        t = 0.0
    loose = Path(p).parent in config.DROP_ZONES
    return zoned(p), loose, plain(p) != Path(p).name, t


def duplicates(store: Store) -> list[dict]:
    """Byte-identical pairs among `engine.dupes`. Look-alikes that differ are left out (video frames,
    licence files), and so are same-named copies inside one project (each variant's settings.json):
    that is the project's layout, not clutter. Two filed copies under different names are offered
    only when one is the other saved again (`x (1).png`): a vault's pasted images are each linked
    from a note by name, and trashing one breaks the note."""
    out = []
    for x in engine.dupes(store, COPY):
        a, b = x["a"], x["b"]
        if Path(a).name == Path(b).name and project(a) == project(b):
            continue
        if same_bytes(a, b):
            keep, extra = sorted((a, b), key=keep_first)
            if zoned(extra) or (plain(extra) == Path(keep).name != Path(extra).name):
                out.append({"id": f"dup:{keep}|{extra}", "kind": "duplicate", "confidence": 1.0, "paths": [keep, extra]})
    return out


def words(name: str) -> list[str]:
    """A folder name as words, case and plural dropped: `Invoices_2023` is invoice, 2023."""
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.split(r"[^0-9a-z]+", name.casefold()) if w]


def related(a: str, b: str) -> str | None:
    """How two folder names mean the same thing: "same" (`alamairis` and `Alamairis`, `invoice` and
    `invoices`), "close" (a typo apart), or None. A numbered series is neither: names that differ in
    a word with a digit in it (`2023-05` and `2023-06`, `shoulder-2-watch` and `shoulder-1c-watch`)."""
    wa, wb = words(a), words(b)
    if wa == wb:
        return "same"
    if any(re.search(r"\d", w) for w in set(wa) ^ set(wb)):
        return None
    return "close" if SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio() >= 0.85 else None


def merges(m: dict, pairs: list[tuple[str, str]] = ()) -> list[dict]:
    """Two folders whose names mean the same thing and whose files with content average to nearly
    the same vector: 0.6 when the names match (1 file or more each), 0.9 when they are a typo apart
    (2 or more each). Their parents' names must mean the same thing too (`dnd/alamairis` and
    `dnd/Alamairis`): `Artoria/textures` and `JacktheRipper/Textures` are how each project lays
    itself out, and `2024/11` and `2025/11` are two months. The smaller goes into the larger.
    Every pair of folders is looked at, and the pairs misfiled() hands over go through the same rules."""
    labeled = engine.labeled(m["paths"])
    held = defaultdict(list)
    for i, p in enumerate(m["paths"]):
        if labeled[i] and m["kinds"][i] != "name":
            held[str(Path(p).parent)].append(i)
    fs = list(held)
    if len(fs) < 2:
        return []
    c = np.stack([m["vecs"][held[f]].mean(axis=0) for f in fs])
    c /= np.linalg.norm(c, axis=1, keepdims=True)
    at = {f: n for n, f in enumerate(fs)}
    cs = c @ c.T
    ij = set(zip(*np.nonzero(np.triu(cs, 1) >= MERGE_SAME)))
    ij |= {tuple(sorted((at[a], at[b]))) for a, b in pairs if a in at and b in at}
    found = {}
    for i, j in ij:
        a, b = fs[i], fs[j]
        how = apart(a, b) and related(Path(a).parent.name, Path(b).parent.name) and related(Path(a).name, Path(b).name)
        least = min(len(held[a]), len(held[b]))
        if (how == "same" and cs[i, j] >= MERGE_SAME) or (how == "close" and cs[i, j] >= MERGE and least >= 2):
            src, dst = sorted((a, b), key=lambda f: len(held[f]))
            found[src, dst] = {"id": f"merge:{src}|{dst}", "kind": "merge", "confidence": round(float(cs[i, j]), 3),
                               "paths": [src, dst], "to": dst}
    return sorted(found.values(), key=lambda p: -p["confidence"])[:MERGES]


def propose(store: Store) -> list[dict]:
    """Every idea, surest first. Group (files about one thing into a new folder) is not built."""
    m = store.mem()
    cards, pairs = misfiled(m)
    return sorted(cards + duplicates(store) + merges(m, pairs), key=lambda p: -p["confidence"])


def touches(p: dict, folder: str) -> bool:
    """The idea moves out of or into folder, or works on folder or something directly inside it."""
    return folder in (p.get("from"), p.get("to")) or any(x == folder or str(Path(x).parent) == folder for x in p["paths"])


def load() -> dict:
    with _state:
        st = json.loads(config.TIDY.read_text()) if config.TIDY.exists() else {}
    return {"skips": st.get("skips", {}), "mutes": st.get("mutes", []), "verdicts": st.get("verdicts", {})}


def save(st: dict) -> None:
    with _state:
        config.TIDY.parent.mkdir(parents=True, exist_ok=True)
        config.TIDY.write_text(json.dumps(st, indent=1))


class Tidy:
    def __init__(self, store: Store, bump):
        self.store, self.bump = store, bump
        self.found: list[dict] = []
        self.ready = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self) -> None:
        while True:
            t = time.time()
            try:
                found = propose(self.store)
                with self.lock:
                    changed, self.found = found != self.found, found
                if changed:
                    self.bump()
                print(f"tidy: {len(found)} ideas in {time.time() - t:.1f}s", flush=True)
                self.judge(found)
            except Exception:
                print(traceback.format_exc(), flush=True)
            self.ready.set()
            self.wake.wait(REFRESH)
            time.sleep(SETTLE)
            self.wake.clear()

    def judge(self, found: list[dict]) -> None:
        """Ask Opus about each idea it has not judged yet, a few at a time. A verdict is kept by
        idea id, so a pass never asks twice; a failed ask leaves the idea unjudged, and hidden,
        until the next pass."""
        todo = [p for p in found if p["id"] not in load()["verdicts"]]
        if not todo:
            return
        if not judge.available():
            print(f"tidy: {len(todo)} ideas unjudged, no claude on PATH", flush=True)
            return
        t, kept, done = time.time(), 0, 0
        with ThreadPoolExecutor(JUDGES) as pool:
            for p, v in zip(todo, pool.map(judge.ask, todo)):
                if v is None:
                    continue
                with _state:
                    st = load()
                    st["verdicts"][p["id"]] = {**v, "at": time.time()}
                    save(st)
                done, kept = done + 1, kept + v["ok"]
        print(f"tidy: judged {done} of {len(todo)}, kept {kept}, in {time.time() - t:.0f}s", flush=True)
        if done:
            self.bump()

    def kick(self) -> None:
        """Something moved: work the ideas out again soon."""
        self.wake.set()

    def list(self, folder: str | None = None) -> list[dict]:
        """The cached ideas Opus said yes to and still worth showing, surest first, each with its
        reason; those touching folder if given. Waits at most a second for the first pass."""
        self.ready.wait(1.0)
        folder = os.path.normpath(folder) if folder else None
        st, now = load(), time.time()
        with self.lock:
            found = list(self.found)
        return [{**p, "reason": st["verdicts"][p["id"]]["reason"]} for p in found
                if st["verdicts"].get(p["id"], {}).get("ok")
                and st["skips"].get(p["id"], 0) < now
                and not any(touches(p, f) for f in st["mutes"])
                and (folder is None or touches(p, folder))
                and all(Path(x).exists() for x in p["paths"])]

    def get(self, pid: str) -> dict:
        with self.lock:
            for p in self.found:
                if p["id"] == pid:
                    return p
        raise LookupError(f"no proposal {pid}")

    def drop(self, pid: str) -> None:
        with self.lock:
            self.found = [p for p in self.found if p["id"] != pid]
        self.bump()

    def skip(self, pid: str) -> None:
        with _state:
            st, now = load(), time.time()
            st["skips"] = {k: t for k, t in st["skips"].items() if t > now}
            st["skips"][pid] = now + QUIET
            save(st)
        self.bump()

    def mute(self, folder: str) -> None:
        with _state:
            st = load()
            folder = os.path.normpath(folder)
            if folder not in st["mutes"]:
                st["mutes"].append(folder)
            save(st)
        self.bump()
