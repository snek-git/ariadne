"""The daemon: holds the model and the index, answers JSON on 127.0.0.1:8199 for the CLI and the
file manager, and watches the drop zones."""
import json
import subprocess
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, decide, engine, inbox, proposals, sizes, watch
from .embed import Embedder
from .extract import ext
from .index import embed_files, index
from .store import Store

LINGER = 6.0  # seconds the status line says what just moved
OWN = 30.0  # seconds a move already accounted for stays known to the watcher check
EXCERPT = 200  # characters of text a peek shows


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
        self.last: dict | None = None  # the newest move, for the status line
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

    def ensure(self, path: str):
        """The file's vector, embedding it first if the index has not seen it."""
        v = self.store.file_vec(path)
        if v is None:
            [(p, kind, rows, fvec)] = embed_files(self.emb, [Path(path)])
            st = p.stat()
            self.store.put(path, st.st_size, st.st_mtime, kind, rows, fvec)
            v = fvec
        return v

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
        self.store.rename(src, dst)
        if self.manual(src, dst):
            if self.inbox.get(src):
                self.inbox.pop(src)
            self.after(inbox.record(src, dst, by="you"))

    def on_gone(self, path: str) -> None:
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

    def where(self, path: str) -> tuple[list[dict], dict]:
        """The vote's ranked folders, and the decision: the vote when sure, else Jev."""
        route = engine.route(self.store, self.ensure(path), exclude=path)
        if self.decider is None:
            return route, {"folder": route[0]["folder"], "share": route[0]["share"], "by": "vote"}
        m = self.store.mem()
        names = decide.members([p for p, ok in zip(m["paths"], engine.labeled(m["paths"])) if ok and p != path])
        return route, self.decider.decide(path, route, self.store.first_text(path), names)

    def dropped(self, path: str) -> None:
        """Confident: move it now (the Inbox lists it with Undo). Unsure, or a copy of something you
        already filed: an Inbox suggestion."""
        if path in self.returned:  # an undo put it back here: leave it alone
            self.returned.discard(path)
            self.ensure(path)
            return
        with self.task(self.reading, path):
            route, d = self.where(path)
            dup = proposals.copy_of(self.store, path)
            if dup or not decide.confident(d):
                self.inbox.suggest(path, route, d, dup)
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

    def keep(self, path: str) -> dict:
        """Leave a suggested file where it is and take it off the Inbox."""
        return {"kept": self.inbox.pop(path) is not None}

    def undo(self, moved_to: str | None = None) -> dict | None:
        """Put back the move that put a file at moved_to (yours or ariadne's), else ariadne's newest."""
        m = inbox.undo_target(moved_to)
        if m is None:
            return None
        self.returned.add(m["from"])  # before the move, so the watcher sees it as returned
        self.mine(m["to"], m["from"])
        inbox.undo(m)
        self.store.rename(m["to"], m["from"])
        self.bump()
        self.tidy.kick()
        return m

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
        ix = self.indexing or {}
        running = bool(ix.get("running"))
        n = (ix.get("left") or 0) * running + embedding
        last = self.last if self.last and time.time() - self.last["at"] < LINGER else None
        if running and ix.get("left") is None:
            line = "indexing files"
        elif n:
            line = f"indexing {n} file{'s' * (n != 1)}"
        elif reading:
            line = f"reading {Path(reading[0]).name}"
        elif last:
            binned = Path(last["to"]).is_relative_to(config.TRASH)
            name = Path(last["from" if binned else "to"]).name
            line = f"moved {name} to {'trash' if binned else Path(last['to']).parent.name}"
        else:
            line = "up to date"
        return {"line": line, "busy": running or bool(embedding or reading)}

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
                    d.ensure(q["path"])
                    return self.send({"hits": engine.similar(d.store, q["path"], int(q.get("n", 12)))})
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
                if self.path == "/undo":
                    return self.send({"undone": d.undo(body.get("path"))})
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
    srv = ThreadingHTTPServer((config.HOST, config.PORT), handler(d))
    print(f"listening on http://{config.HOST}:{config.PORT}", flush=True)
    srv.serve_forever()
