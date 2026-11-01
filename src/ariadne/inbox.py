"""The Inbox and the moves log. Suggestions wait for Move or Keep; files moved on their own stay
listed for a day with Undo. Every move, ariadne's or yours, is a line in the log; undo marks the
line instead of deleting it, so a move's id (its line number) never changes. `version` goes up on
every change to the Inbox, the moves, the status line or the proposals, so the window can poll one
cheap number."""
import json
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import config

RECENT = 24 * 3600  # how long a file moved on its own stays in the Inbox
ORIGIN = "user.xdg.origin.url"  # set by browsers on downloaded files
LOG_LOCK = threading.Lock()


class Inbox:
    def __init__(self):
        self.pending: dict[str, dict] = {}  # path -> {"path", "route", "decision", "at", "source", "dup_of"}
        self.version = int(time.time())  # starts past any earlier run's count, so a restart reads as a change
        self.lock = threading.Lock()

    def touch(self) -> None:
        with self.lock:
            self.version += 1

    def suggest(self, path: str, route: list[dict], decision: dict, dup_of: str | None) -> None:
        with self.lock:
            self.pending[path] = {"path": path, "route": route[:5], "decision": decision, "at": time.time(),
                                  "source": source(path), "dup_of": dup_of}
            self.version += 1

    def get(self, path: str) -> dict | None:
        with self.lock:
            return self.pending.get(path)

    def pop(self, path: str) -> dict | None:
        with self.lock:
            self.version += 1
            return self.pending.pop(path, None)

    def suggestions(self) -> list[dict]:
        with self.lock:
            return sorted(self.pending.values(), key=lambda d: -d["at"])

    def items(self) -> list[dict]:
        """Suggested and moved files, newest first (the daemon adds the ones being read)."""
        out = []
        for s in self.suggestions():
            if Path(s["path"]).exists():
                d = s["decision"]
                out.append({"state": "suggested", "path": s["path"], "folder": d["folder"], "share": d["share"],
                            "by": d["by"], "fits": d.get("fits"), "at": s["at"], "source": s["source"],
                            "dup_of": alive(s["dup_of"]), "alternatives": others(s["route"], d["folder"])})
        for m in log():
            if m.get("auto") and not m.get("undone") and time.time() - m["at"] < RECENT and Path(m["to"]).exists():
                out.append({"state": "moved", "path": m["to"], "from": m["from"], "folder": str(Path(m["to"]).parent),
                            "share": m.get("share"), "by": m.get("by"), "fits": m.get("fits"), "at": m["at"],
                            "source": m.get("source"), "dup_of": alive(m.get("dup_of")),
                            "alternatives": m.get("alternatives", [])})
        return sorted(out, key=lambda d: -d["at"])


def others(route: list[dict], folder: str) -> list[dict]:
    """The vote's other folders, best first."""
    return [{"folder": r["folder"], "share": r["share"]} for r in route if r["folder"] != folder][:4]


def alive(path: str | None) -> str | None:
    return path if path and Path(path).exists() else None


def source(path: str) -> str:
    """Where a file in a drop zone came from: the site it was downloaded from, else the zone's name
    ("Telegram Desktop" reads "Telegram")."""
    try:
        host = urlparse(os.getxattr(path, ORIGIN).decode()).hostname
        if host:
            return host.removeprefix("www.")
    except OSError:
        pass
    return Path(path).parent.name.removesuffix(" Desktop")


def log() -> list[dict]:
    """Every move, oldest first, each with its id."""
    if not config.MOVES.exists():
        return []
    with LOG_LOCK:
        lines = config.MOVES.read_text().splitlines()
    return [{**json.loads(line), "id": i} for i, line in enumerate(lines, 1)]


def thread(limit: int = 40) -> list[dict]:
    """The undo thread: moves not undone whose file is still where they put it, oldest first."""
    live = [m for m in log() if not m.get("undone") and Path(m["to"]).exists()][-limit:]
    return [knot(m) for m in live]


def knot(m: dict) -> dict:
    you = m.get("by") == "you"
    return {"id": m["id"], "at": m["at"], "from": m["from"], "to": m["to"], "by": "you" if you else "ariadne",
            "share": m.get("share"), "learned": you}


def free(folder: str, name: str) -> str:
    """A path in folder for name that nothing uses yet: `x.pdf`, else `x (1).pdf` and on."""
    dst, n = Path(folder) / name, 1
    while dst.exists():
        dst = Path(folder) / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return str(dst)


def record(src: str, dst: str, **entry) -> dict:
    """Append a move to the log; returns it with its id."""
    entry = {"at": time.time(), "from": src, "to": dst, **entry}
    config.MOVES.parent.mkdir(parents=True, exist_ok=True)
    with LOG_LOCK:
        n = len(config.MOVES.read_text().splitlines()) if config.MOVES.exists() else 0
        with config.MOVES.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    return {**entry, "id": n + 1}


def move(src: str, dst: str, **entry) -> dict:
    shutil.move(src, dst)
    return record(src, dst, **entry)


def undo_target(moved_to: str | None = None) -> dict | None:
    """The move undo() would put back: the one that put a file at moved_to, else ariadne's newest."""
    for m in reversed(log()):
        if m.get("undone"):
            continue
        if (m["to"] == moved_to) if moved_to else m.get("by") != "you":
            return m
    return None


def undo(m: dict) -> None:
    """Put a move back and mark its line undone."""
    Path(m["from"]).parent.mkdir(parents=True, exist_ok=True)  # a merge removed the source folder
    shutil.move(m["to"], m["from"])
    if Path(m["to"]).is_relative_to(config.TRASH / "files"):
        (config.TRASH / "info" / f"{Path(m['to']).name}.trashinfo").unlink(missing_ok=True)
    with LOG_LOCK:
        lines = config.MOVES.read_text().splitlines()
        entry = json.loads(lines[m["id"] - 1])
        entry["undone"] = time.time()
        lines[m["id"] - 1] = json.dumps(entry)
        config.MOVES.write_text("".join(line + "\n" for line in lines))


def trashed(path: str) -> str:
    """Where `gio trash` put path: the newest trash entry whose original path is path."""
    info = config.TRASH / "info"
    hits = []
    for f in info.glob("*.trashinfo"):
        for line in f.read_text(errors="replace").splitlines():
            if line.startswith("Path=") and unquote(line[5:]) == path:
                hits.append(f)
    if not hits:
        raise FileNotFoundError(f"{path} is not in the trash")
    newest = max(hits, key=lambda f: f.stat().st_mtime)
    return str(config.TRASH / "files" / newest.name.removesuffix(".trashinfo"))
