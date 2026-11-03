"""The daemon: holds the model and the index, answers JSON on 127.0.0.1:8199 for the CLI and the
file manager, and watches the drop zones."""
import json
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, decide, engine, inbox, organize, proposals, sizes, watch
from .embed import Embedder
from .extract import ext
from .index import embed_files, index
from .store import Store

LINGER = 6.0  # seconds the status line says what just moved
OWN = 30.0  # seconds a move already accounted for stays known to the watcher check
EXCERPT = 200  # characters of text a peek shows
DECIDED = 3600  # seconds a cached routing decision holds
ROUTERS = 4  # files a plan routes at once (Jev calls in flight); the TypeSafe client is thread safe
ALTERNATIVES = 4  # other folders a plan row offers for Send to


class Daemon:
    def __init__(self):
        t = time.time()
        self.store = Store()
        self.emb = Embedder()
        self.inbox = inbox.Inbox()
        self.indexing: dict | None = None
        self.log = []
        self.returned: set[str] = set()  # paths an undo put back in a drop zone
        self.reading: dict[str, float] = {}  # drop zone files being decided on -> since
        self.embedding: dict[str, float] = {}  # files the watcher handed in, being embedded -> since
        self.own: dict[tuple[str, str], float] = {}  # (from, to) of moves already logged -> when
        self.last: dict | None = None  # the newest move, or a line about a whole action, for the status line
        self.planning: list[dict] = []  # {"done", "total"} of each Organize plan routing its files right now
        self.routed: dict[str, tuple] = {}  # path -> ((size, mtime, index changes), when, decision)
        self.lock = threading.Lock()
        try:
            self.decider = decide.Decider()
        except RuntimeError as err:  # no key: the vote decides alone
            self.decider = None
            print(f"jev off: {err}", flush=True)
        self.tidy = proposals.Tidy(self.store, self.bump)
        self.sizes = sizes.Sizes(self.bump)
        print(f"model loaded in {time.time() - t:.1f}s, {self.store.count()} files indexed", flush=True)

    def bump(self) -> None:
        """One version for the Inbox, the moves, the status line and the proposals."""
        self.inbox.touch()

    def say(self, line: str) -> None:
        print(line, flush=True)
        self.log = (self.log + [line])[-50:]

    def reindex(self) -> bool:
        if self.indexing and self.indexing.get("running"):
            return False
        self.indexing = {"running": True, "started": time.time(), "left": None}
        self.bump()

        def left(n: int) -> None:
            self.indexing["left"] = n
            self.bump()

        def job():
            try:
                self.indexing.update(index(self.store, self.emb, log=self.say, left=left))
            except Exception:
                self.say(traceback.format_exc())
            self.indexing["running"] = False
            self.bump()
            self.tidy.kick()
        threading.Thread(target=job, daemon=True).start()
        return True

    @contextmanager
    def task(self, group: dict[str, float], path: str):
        """Mark a file as being read or embedded while the block runs, for the status line."""
        with self.lock:
            group[path] = time.time()
        self.bump()
        try:
            yield
        finally:
            with self.lock:
                group.pop(path, None)
            self.bump()

    def ensure(self, path: str) -> dict:
        """The file as the index sees it (vector, kind, first text chunk), embedding it first if the
        index has not seen it. Only a file the index keeps is stored: one picked outside the roots
        would stay behind as a stale example that votes."""
        v = self.store.file_vec(path)
        if v is not None:
            return {"vec": v, "kind": self.store.kind(path), "text": self.store.first_text(path)}
        [(p, kind, rows, fvec)] = embed_files(self.emb, [Path(path)])
        if watch.indexable(p):
            st = p.stat()
            self.store.put(path, st.st_size, st.st_mtime, kind, rows, fvec)
        text = rows[1][1] if len(rows) > 1 and rows[1][0] == "chunk" else None
        return {"vec": fvec, "kind": kind, "text": text}

    def refresh(self, path: str) -> None:
        """Re-embed a file whose content changed."""
        for p, kind, rows, fvec in embed_files(self.emb, [Path(path)]):
            st = p.stat()
            self.store.put(path, st.st_size, st.st_mtime, kind, rows, fvec)

    # What the watcher reports. Outside the drop zones ariadne only keeps the index current and
    # notes the moves you make.
    def on_new(self, path: str) -> None:
        if Path(path).parent in config.DROP_ZONES:
            self.dropped(path)
        elif self.store.file_vec(path) is None:  # a moved file already has its vector
            with self.task(self.embedding, path):
                self.ensure(path)
            self.tidy.kick()

    def on_changed(self, path: str) -> None:
        with self.task(self.embedding, path):
            self.refresh(path)
        self.tidy.kick()

    def on_moved(self, src: str, dst: str) -> None:
        # Events arrive late: after a quick Move then Undo, the Move's event lands with the file back
        # at src. Renaming then would point its vectors at a path that is gone.
        if Path(src).exists() and not Path(dst).exists():
            return
        self.store.rename(src, dst)
        if self.manual(src, dst):
            if self.inbox.get(src):
                self.inbox.pop(src)
            self.after(inbox.record(src, dst, by="you"))

    def on_gone(self, path: str) -> None:
        if Path(path).exists():  # a late event: an Undo already put it back
            return
        self.store.remove(path)

    def manual(self, src: str, dst: str) -> bool:
        """A move you made: to another folder (not a rename in place), between visible places (the
        trash is hidden or outside the roots, so moves into or out of it never count), and not one
        ariadne made or already logged. A folder's move covers the events for everything inside."""
        if Path(src).parent == Path(dst).parent or not (watch.visible(Path(src)) and watch.visible(Path(dst))):
            return False
        now = time.time()
        with self.lock:
            self.own = {k: t for k, t in self.own.items() if now - t < OWN}
            for a, b in self.own:
                if (src, dst) == (a, b) or (src.startswith(a + "/") and dst.startswith(b + "/")):
                    return False
            self.own[(src, dst)] = now
        return True

    def mine(self, src: str, dst: str) -> None:
        """Note a move before making it, so the watcher does not log it as yours."""
        with self.lock:
            self.own[(src, dst)] = time.time()

    def after(self, m: dict) -> None:
        """A move landed in the log: the status line says so for a few seconds."""
        self.last = m
        self.bump()
        threading.Timer(LINGER, self.bump).start()
        self.tidy.kick()

    def move(self, src: str, folder: str, **entry) -> dict:
        """Every move ariadne makes: logged (so it can be undone and shows in /moves), index updated."""
        dst = inbox.free(folder, Path(src).name)
        self.mine(src, dst)
        m = inbox.move(src, dst, **entry)
        self.store.rename(src, dst)
        self.after(m)
        return m

    def trash(self, path: str, **entry) -> dict:
        """Move a file to the freedesktop trash, logged like any move so Undo restores it."""
        subprocess.run(["gio", "trash", path], check=True, capture_output=True)
        m = inbox.record(path, inbox.trashed(path), **entry)
        self.store.remove(path)
        self.after(m)
        return m

    def where(self, path: str, homes: bool = False) -> tuple[list[dict], dict]:
        """The vote's ranked folders, and the decision: the vote when sure, else Jev. `homes` (an
        Organize plan) leaves the Downloads piles out of the vote."""
        f = self.ensure(path)
        route = engine.route(self.store, f["vec"], exclude=path, homes=homes)
        m = engine.prepared(self.store)
        own = path in m["at"] and m["labeled"][m["at"][path]]  # a file already filed is no example of itself
        filed = [p for p in m["filed"] if p != path] if own else m["filed"]
        if own:
            names = decide.members(filed)
        else:
            if "members" not in m:
                m["members"] = decide.members(filed)
            names = m["members"]
        if self.decider is None:
            d = {"folder": route[0]["folder"], "share": route[0]["share"], "by": "vote"}
        else:
            d = self.decider.decide(path, route, f["text"], names)
        # what decide.confident needs to doubt a name-only file whose nearest match is weak, or a
        # folder the file's name and type give no reason for
        g = decide.grounds(path, d["folder"], filed)
        if not g["held"]:  # the pick holds nothing of its type: the best other route folder that does
            for r in route:
                if r["folder"] != d["folder"] and (rg := decide.grounds(path, r["folder"], filed))["held"]:
                    d, g = {"folder": r["folder"], "share": r["share"], "by": "vote", "runner_up": True}, rg
                    break
        return route, {**d, "named": f["kind"] == "name", "top": route[0]["top"], **g}

    def filed(self) -> list[str]:
        """Indexed files that sit in a folder you made."""
        return engine.prepared(self.store)["filed"]

    def dropped(self, path: str, at: float | None = None, move: bool = True) -> None:
        """Confident: move it now (the Inbox lists it with Undo). Unsure, or a copy of something you
        already filed: an Inbox suggestion. `at` is when it arrived, for files from before a start;
        `move` false makes even a sure file only a suggestion."""
        if path in self.returned:  # an undo put it back here: leave it alone
            self.returned.discard(path)
            self.ensure(path)
            return
        with self.task(self.reading, path):
            route, d = self.where(path)
            dup = proposals.copy_of(self.store, path)
            why = decide.groundless(d)
            if why and not dup:  # no real guess: it stays, and the Inbox shows no made-up folder
                self.say(f"drop zone: left {Path(path).name}: {why} (closest: {d['folder']})")
                return
            if dup or not move or not decide.confident(d):
                self.inbox.suggest(path, route, d, dup, at)
                self.say(f"drop zone: {Path(path).name} {decide.summary(d)}" + (f", a copy of {dup}" if dup else ""))
                return
            self.move(path, d["folder"], auto=True, share=d["share"], by=d["by"], fits=d.get("fits"),
                      source=inbox.source(path), alternatives=inbox.others(route, d["folder"]))
            self.say(f"drop zone: moved {Path(path).name} -> {d['folder']} ({d['share']:.0%}, by {d['by']})")

    def accept(self, path: str, folder: str | None) -> dict:
        """Move a file where it was suggested, or to the folder you picked instead: that is a
        correction, logged as yours (learned)."""
        s = self.inbox.get(path)
        d = s["decision"] if s else None
        if folder is None:
            d = d or self.where(path)[1]
            if d.get("fits", 1.0) < decide.NONE_FITS:
                raise ValueError("no folder fits this file; pick one with --to")
            folder = d["folder"]
        sure = d and d["folder"] == folder
        m = self.move(path, folder, **({"share": d["share"], "by": d["by"]} if sure else {"by": "you"}))
        self.inbox.pop(path)
        return {"from": path, "to": m["to"]}

    def cached(self, what: str, path: str, work):
        """work(path), cached: Jev answers differently from call to call and takes a second or two,
        so a plan asked again gives the same answer at once. An entry holds while the file (size,
        mtime) and the index stay as they were, for at most DECIDED seconds."""
        st = Path(path).stat()
        key = (st.st_size, st.st_mtime, self.store.changes)
        with self.lock:
            hit = self.routed.get((what, path))
        if hit and hit[0] == key and time.time() - hit[1] < DECIDED:
            return hit[2]
        out = work(path)
        with self.lock:
            self.routed[what, path] = (key, time.time(), out)
        return out

    def nearby(self, path: str) -> list[str]:
        """The folders the vote ranks for a file, best first, for the panel's Send to list. The vote
        only: no Jev, and no embedding (a file the index lacks has none)."""
        def vote(p: str) -> list[str]:
            v = self.store.file_vec(p)
            if v is None:
                return []
            return list(dict.fromkeys(r["folder"] for r in engine.route(self.store, v, exclude=p, homes=True)))
        try:
            return self.cached("near", path, vote)
        except OSError:  # gone since the plan looked
            return []

    def decided(self, path: str) -> dict:
        return self.cached("where", path, lambda p: self.where(p, homes=True)[1])

    def again(self, paths: list[Path], planned: dict[str, str], filling: set[str]) -> dict[Path, dict | None]:
        """Decisions for these files as if a plan's ticked files (path -> folder) were already moved:
        the vote among your own folders, and Jev's pick when the vote is unsure and a folder the file
        may join (`filling`) is on its shortlist. Nothing here is cached, as those folders may never be
        made; Jev's own memo still gives one shortlist one answer, so a plan asked again comes out the same."""
        filed = [p for p in engine.prepared(self.store)["filed"] if p not in planned]
        names = decide.members(filed + [str(Path(f) / Path(p).name) for p, f in planned.items()])

        def one(p: Path) -> tuple[Path, dict | None]:
            v = self.store.file_vec(str(p))
            if v is None:
                return p, None
            route = engine.route(self.store, v, exclude=str(p), homes=True, planned=planned)
            d = {"folder": route[0]["folder"], "share": route[0]["share"], "by": "vote"}
            if self.decider and filling & {r["folder"] for r in route[:decide.SHORTLIST]}:
                d = self.decider.decide(str(p), route, self.store.first_text(str(p)), names)
            return p, {**d, "named": organize.kind(p) == "name", "top": route[0]["top"]}
        with ThreadPoolExecutor(ROUTERS) as pool:
            return dict(pool.map(one, paths))

    def plan(self, paths: list[str]) -> dict:
        """What Organize would do with the files you picked, grouped by folder. Moves nothing. First
        each file's filed copy is looked up, then the files no rule settles are routed, a few at a
        time (Jev answers over the network); /activity counts the routing."""
        kept = inbox.kept()
        files, left = organize.expand(paths, kept)
        ghosts = organize.siblings(files, kept)
        shelf = organize.Shelf(self.filed())
        with ThreadPoolExecutor(ROUTERS) as pool:
            copies = dict(zip(files, pool.map(
                lambda p: self.cached("copy", str(p), lambda x: proposals.copy_of(self.store, x)), files)))
        todo = organize.needs(files + ghosts, shelf, copies)
        n = len(files) + len(ghosts)
        progress = {"done": n - len(todo), "total": n}  # this plan's own count
        with self.lock:
            self.planning.append(progress)
        self.bump()
        decisions: dict[Path, dict] = {}

        def route(p: Path) -> None:
            decisions[p] = self.decided(str(p))
            with self.lock:
                progress["done"] += 1
            self.bump()
        try:
            with ThreadPoolExecutor(ROUTERS) as pool:
                list(pool.map(route, todo))
            out = organize.plan(files, shelf, lambda p: decisions.get(p) or self.decided(str(p)), copies,
                                inbox.rejected(), inbox.renamed(), left, ghosts, self.again)
        finally:
            with self.lock:
                self.planning.remove(progress)
            self.bump()

        def others(path: str, own: str | None) -> list[str]:
            """Folders ariadne may offer, outside Downloads, already holding files of this type
            directly: a phone photo is offered no doujin folder or 3D print project of other types."""
            return [x for x in self.nearby(path) if x != own and shelf.proposable(x, Path(path))][:ALTERNATIVES]
        for g in out["groups"]:
            for f in g["files"]:
                f["alternatives"] = others(f["path"], g["folder"])
        for x in out["left"]:
            x["alternatives"] = others(x["path"], None)
        self.say(f"organize: planned {len(files)} files into {len(out['groups'])} folders, left {len(out['left'])}")
        return out

    def apply_plan(self, groups: list[dict], trash: list[str] = ()) -> dict:
        """Carry out a plan as you ticked it, as one batch so one Undo puts it all back. A name
        clash is never overwritten. A file you unticked (`declined`) is remembered as a no for the
        folder it was proposed for, and a folder you renamed as your name for that proposal. A group
        inside a folder you renamed moves with it (papers/cv follows papers -> paperwork) unless it
        says `carry: false`; either way the batch records the child's own folder, so the next plan
        and an Undo both know. The copies in `trash` go to the trash in the same batch, each only
        while another copy of it stays. The batch remembers the folders it made, the noes and the
        renames, for its Undo."""
        batch = f"organize-{time.time():.3f}"
        moved, left, no, into, made, binned = [], [], [], set(), [], []
        renames = {g["proposed"]: g["folder"] for g in groups if g.get("proposed") not in (None, g["folder"])}
        for g in sorted(groups, key=lambda g: len(g["folder"])):  # a parent's choice before its children's
            if g.get("proposed") and g["folder"] == g["proposed"] and \
                    (carried := organize.named_as(g["folder"], renames)) != g["folder"]:
                renames[g["proposed"]] = carried if g.get("carry", True) else g["folder"]
        inbox.rename(renames)
        for g in groups:
            folder = Path(renames.get(g["folder"], g["folder"]) if g["folder"] == g.get("proposed") else g["folder"])
            if not folder.is_absolute():
                raise ValueError(f"not an absolute folder: {folder}")
            for f in g["files"]:
                path = Path(f["path"])
                if not f.get("ticked"):
                    if f.get("declined"):
                        no.append((str(path), g.get("proposed") or str(folder)))
                    continue
                if not path.is_file():
                    why = "it is gone"
                elif path.parent == folder:
                    why = "already there"
                elif (folder / path.name).exists() or (folder.exists() and not folder.is_dir()):
                    why = "a file with this name is already there"
                else:
                    made += [str(a) for a in reversed([folder, *folder.parents]) if not a.exists()]
                    folder.mkdir(parents=True, exist_ok=True)
                    moved.append(inbox.knot(self.move(str(path), str(folder), batch=batch, by="you")))
                    into.add(folder)
                    if self.inbox.get(str(path)):
                        self.inbox.pop(str(path))
                    continue
                left.append({"path": str(path), "why": why})
        gone = set(trash)
        for path in trash:
            if not Path(path).is_file():
                why = "it is gone"
            elif not (copy := self.kept_copy(path, gone)):
                why = "no other copy of it is left"
            else:
                binned.append(inbox.knot(self.trash(path, batch=batch, by="you", copy=copy)))
                if self.inbox.get(path):
                    self.inbox.pop(path)
                continue
            gone.discard(path)  # it stays, so it may be the copy another one keeps
            left.append({"path": path, "why": why})
        inbox.reject(no)
        inbox.remember(batch, made, no, renames)
        if moved or binned:
            what = [f"organized {organize.files(len(moved))} into {len(into)} folder{'s' * (len(into) != 1)}"] * bool(moved)
            what += [f"trashed {len(binned)} cop{'ies' if len(binned) != 1 else 'y'}"] * bool(binned)
            self.tell(", ".join(what))
        self.say(f"organize: moved {len(moved)}, trashed {len(binned)}, left {len(left)}, said no to {len(no)}")
        return {"batch": batch, "moved": moved, "trashed": [m["from"] for m in binned], "left": left}

    def kept_copy(self, path: str, gone: set[str]) -> str | None:
        """Another copy of this file that stays where it is: the same bytes anywhere ariadne
        indexed, or the look-alike a plan called its copy. None when every copy is going."""
        for p in self.store.sized(Path(path).stat().st_size):
            if p != path and p not in gone and Path(p).is_file() and proposals.same_bytes(path, p):
                return p
        c = proposals.copy_of(self.store, path)
        return c if c and c not in gone and Path(c).is_file() else None

    def tell(self, line: str) -> None:
        """Put one line about a whole action on the status line for a few seconds."""
        self.last = {"at": time.time(), "line": line}
        self.bump()
        threading.Timer(LINGER, self.bump).start()

    def keep(self, path: str) -> dict:
        """Leave a suggested file where it is and take it off the Inbox, for good."""
        inbox.keep(path)
        return {"kept": self.inbox.pop(path) is not None}

    def backlog(self) -> None:
        """On start, handle the last month of drop zone files as if they had just arrived, except
        the ones you kept or an undo put back. Newest first, so the Inbox fills from the top. Only a
        file the index has not seen as it is may move on its own; one it knows comes back as a
        suggestion. ctime is no proof of a new arrival: the sandbox's hardlinks touch it too, and a
        restart once moved 29 old Telegram files that way."""
        skip = inbox.kept() | inbox.returned()
        known = self.store.known()
        now = time.time()
        files = []
        for zone in config.DROP_ZONES:
            if not zone.is_dir():
                continue
            for p in zone.iterdir():
                if not p.is_file() or not watch.indexable(p) or str(p) in skip:
                    continue
                st = p.stat()
                fresh = known.get(str(p)) != (st.st_size, st.st_mtime)
                # ctime moves when a file lands in the folder, but also on any hardlink: for a file the
                # index already knows here, only its mtime says when it came
                at = max(st.st_mtime, st.st_ctime) if fresh else st.st_mtime
                if now - at < config.BACKLOG:
                    files.append((at, str(p), fresh))
        self.say(f"backlog: {len(files)} drop zone file{'s' * (len(files) != 1)} from the last 30 days")
        for at, path, fresh in sorted(files, reverse=True):
            if not Path(path).exists() or self.inbox.get(path):  # moved or handled meanwhile
                continue
            try:
                self.dropped(path, at, move=fresh)
            except Exception as err:
                self.say(f"backlog: {Path(path).name} failed: {err!r}")

    def undo(self, moved_to: str | None = None) -> dict:
        """Put back the move that put a file at moved_to, else the newest move of any kind. A move
        from an Organize batch takes its whole batch back with it, so one Ctrl+Z undoes one Organize."""
        m = inbox.undo_target(moved_to)
        if m is None:
            return {"undone": [], "count": 0, "batch": None}
        if m.get("batch"):
            return self.undo_batch(m["batch"])
        self.put_back(m)
        self.tell(f"put back {Path(m['from']).name}")
        self.tidy.kick()
        return {"undone": [inbox.knot(m)], "count": 1, "batch": None}

    def undo_batch(self, batch: str) -> dict:
        """Put back every move of one Organize, newest first, then take back the rest of it: the
        folders it made (when empty again, deepest first), its noes and its renames. The next plan
        then looks like the one before it."""
        done = [m for m in reversed(inbox.log()) if m.get("batch") == batch and not m.get("undone")]
        for m in done:
            self.put_back(m)
        for folder in sorted(inbox.forget(batch), key=lambda f: -len(Path(f).parts)):
            try:
                Path(folder).rmdir()
            except OSError:  # gone, or something else is in it now
                pass
        if done:  # an Undo of a batch already undone says nothing
            self.tell(f"put back {organize.files(len(done))}")
            self.tidy.kick()
        return {"undone": [inbox.knot(m) for m in done], "count": len(done), "batch": batch}

    def put_back(self, m: dict) -> None:
        self.returned.add(m["from"])  # before the move, so the watcher sees it as returned
        self.mine(m["to"], m["from"])
        inbox.undo(m)
        if watch.indexable(Path(m["from"])):
            self.store.rename(m["to"], m["from"])
        else:  # it came from outside the roots (an Organize in ~/dev, say): the index lets it go
            self.store.remove(m["to"])
        self.inbox.pop(m["from"])  # a suggestion for a copy that sat at that path is stale now

    def items(self) -> list[dict]:
        """The Inbox, with the files still being read on top."""
        out = self.inbox.items()
        shown = {i["path"] for i in out}
        with self.lock:
            reading = sorted(self.reading.items(), key=lambda kv: -kv[1])
        return [{"state": "reading", "path": p, "folder": None, "share": None, "fits": None, "by": None, "at": t,
                 "source": inbox.source(p), "dup_of": None, "alternatives": []}
                for p, t in reading if p not in shown] + out

    def activity(self) -> dict:
        """The status line: indexing, then reading, then what just moved, else up to date."""
        with self.lock:
            reading, embedding = list(self.reading), len(self.embedding)
            # plans running at once count as one: their files added up
            planning = {k: sum(x[k] for x in self.planning) for k in ("done", "total")} if self.planning else None
        ix = self.indexing or {}
        running = bool(ix.get("running"))
        n = (ix.get("left") or 0) * running + embedding
        last = self.last if self.last and time.time() - self.last["at"] < LINGER else None
        if planning:
            line = f"planning {planning['done']} of {planning['total']}"
        elif running and ix.get("left") is None:
            line = "indexing files"
        elif n:
            line = f"indexing {n} file{'s' * (n != 1)}"
        elif reading:
            line = f"reading {Path(reading[0]).name}"
        elif last and "line" in last:
            line = last["line"]
        elif last:
            binned = Path(last["to"]).is_relative_to(config.TRASH)
            name = Path(last["from" if binned else "to"]).name
            line = f"moved {name} to {'trash' if binned else Path(last['to']).parent.name}"
        else:
            line = "up to date"
        return {"line": line, "busy": running or bool(embedding or reading or planning), "planning": planning}

    def apply(self, pid: str) -> dict:
        """Carry out a proposal through the same logged moves, so each step can be undone."""
        p = self.tidy.get(pid)
        sure = {"share": p["confidence"]}
        if p["kind"] == "misfiled":
            moves = [self.move(f, p["to"], **sure) for f in p["paths"]]
        elif p["kind"] == "duplicate":
            moves = [self.trash(p["paths"][1], **sure)]
        elif p["kind"] == "merge":
            src = Path(p["paths"][0])
            moves = [self.move(str(c), p["to"], **sure) for c in sorted(src.iterdir())]
            if not any(src.iterdir()):  # something new landed in it meanwhile: leave it
                src.rmdir()
        else:
            raise ValueError(f"cannot apply a {p['kind']} proposal")
        self.tidy.drop(pid)
        return {"moves": [inbox.knot(m) for m in moves]}

    def peek(self, path: str) -> dict:
        p = Path(path)
        text = self.store.first_text(path)
        return {"kind": "folder" if p.is_dir() else ext(p) or "file",
                "excerpt": " ".join(text.split())[:EXCERPT] if text else None, "facts": None, "rename": None}

    def summary(self, path: str) -> dict:
        path = str(Path(path).expanduser())
        return {"path": path, "text": decide.overrides().get(path)}


def handler(d: Daemon):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            v = d.inbox.version  # read first: a change while answering shows on the next poll
            try:
                if u.path == "/status":
                    return self.send({"files": d.store.count(), "indexing": d.indexing, "log": d.log[-5:]})
                if u.path == "/search":
                    t = time.time()
                    vec = d.emb.texts([q["q"]], query=True)[0]
                    hits = engine.search(d.store, vec, int(q.get("n", 20)), q.get("under"))
                    return self.send({"took": round(time.time() - t, 3), "hits": hits})
                if u.path == "/similar":
                    v = d.ensure(q["path"])["vec"]
                    return self.send({"hits": engine.similar(d.store, v, q["path"], int(q.get("n", 12)))})
                if u.path == "/route":
                    route, decision = d.where(q["path"])
                    return self.send({"route": route[:5], "decision": decision})
                if u.path == "/dupes":
                    return self.send({"pairs": engine.dupes(d.store, float(q.get("threshold", 0.97)), q.get("under"))})
                if u.path == "/suggestions":
                    return self.send({"suggestions": d.inbox.suggestions()})
                if u.path == "/inbox":
                    if q.get("version_only"):
                        return self.send({"version": v})
                    return self.send({"version": v, "items": d.items()})
                if u.path == "/moves":
                    return self.send({"version": v, "moves": inbox.thread(int(q.get("limit", 40)))})
                if u.path == "/activity":
                    return self.send({"version": v, **d.activity()})
                if u.path == "/proposals":
                    return self.send({"version": v, "proposals": d.tidy.list(q.get("folder"))})
                if u.path == "/summary":
                    return self.send(d.summary(q["path"]))
                if u.path == "/peek":
                    return self.send(d.peek(q["path"]))
                if u.path == "/sizes":
                    return self.send(d.sizes.get(q["under"], int(q.get("depth", 3))))
                self.send({"error": "not found"}, 404)
            except KeyError as e:
                self.send({"error": f"missing {e}"}, 400)
            except ValueError as e:
                self.send({"error": str(e)}, 400)
            except Exception as e:
                self.send({"error": repr(e), "trace": traceback.format_exc()}, 500)

        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            try:
                if self.path == "/index":
                    return self.send({"started": d.reindex()})
                if self.path == "/accept":
                    return self.send(d.accept(body["path"], body.get("folder")))
                if self.path == "/keep":
                    return self.send(d.keep(body["path"]))
                if self.path == "/organize/plan":
                    return self.send(d.plan(body["paths"]))
                if self.path == "/organize/apply":
                    return self.send(d.apply_plan(body["groups"], body.get("trash", [])))
                if self.path == "/undo":
                    if "batch" in body:
                        return self.send(d.undo_batch(body["batch"]))
                    return self.send(d.undo(body.get("path")))
                if self.path == "/proposals/apply":
                    return self.send(d.apply(body["id"]))
                if self.path == "/proposals/skip":
                    d.tidy.skip(body["id"])
                    return self.send({"skipped": body["id"]})
                if self.path == "/mute":
                    d.tidy.mute(body["folder"])
                    return self.send({"muted": body["folder"]})
                if self.path == "/summary":
                    decide.set_override(body["path"], body["text"])
                    return self.send(d.summary(body["path"]))
                self.send({"error": "not found"}, 404)
            except KeyError as e:
                self.send({"error": f"missing {e}"}, 400)
            except (LookupError, ValueError) as e:
                self.send({"error": str(e)}, 400)
            except Exception as e:
                self.send({"error": repr(e), "trace": traceback.format_exc()}, 500)
    return H


def serve() -> None:
    d = Daemon()
    watch.watch(d.on_new, d.on_changed, d.on_moved, d.on_gone)
    threading.Thread(target=d.backlog, daemon=True).start()
    srv = ThreadingHTTPServer((config.HOST, config.PORT), handler(d))
    print(f"listening on http://{config.HOST}:{config.PORT}", flush=True)
    srv.serve_forever()
