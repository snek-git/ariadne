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
QUIET = 30 * 24 * 3600  # how long a file you unticked in an Organize plan stays off that folder
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

    def suggest(self, path: str, route: list[dict], decision: dict, dup_of: str | None,
                at: float | None = None) -> None:
        with self.lock:
            self.pending[path] = {"path": path, "route": route[:5], "decision": decision, "at": at or time.time(),
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


def kept() -> set[str]:
    """Drop zone files you chose Keep here on, so a restart does not ask about them again."""
    try:
        return set(json.loads(config.KEPT.read_text()))
    except (OSError, ValueError):
        return set()


def keep(path: str) -> None:
    config.KEPT.parent.mkdir(parents=True, exist_ok=True)
    config.KEPT.write_text(json.dumps(sorted(kept() | {path})))


def rejected() -> dict[str, dict[str, float]]:
    """File -> folder -> when you unticked it in an Organize plan, for the last QUIET seconds: the
    plan does not propose that folder for that file again meanwhile."""
    try:
        st = json.loads(config.REJECTED.read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    live = {p: {f: t for f, t in fs.items() if now - t < QUIET} for p, fs in st.items()}
    return {p: fs for p, fs in live.items() if fs}


def reject(pairs: list[tuple[str, str]]) -> None:
    if not pairs:
        return
    st, now = rejected(), time.time()
    for path, folder in pairs:
        st.setdefault(path, {})[folder] = now
    config.REJECTED.parent.mkdir(parents=True, exist_ok=True)
    config.REJECTED.write_text(json.dumps(st, indent=1))


def renamed() -> dict[str, str]:
    """Folder an Organize plan proposed -> the name you gave it instead, so later plans use yours."""
    try:
        return json.loads(config.RENAMED.read_text())
    except (OSError, ValueError):
        return {}


def rename(pairs: dict[str, str]) -> None:
    if pairs:
        config.RENAMED.parent.mkdir(parents=True, exist_ok=True)
        config.RENAMED.write_text(json.dumps({**renamed(), **pairs}, indent=1))


def _batches() -> dict[str, dict]:
    try:
        return json.loads(config.BATCHES.read_text())
    except (OSError, ValueError):
        return {}


def remember(batch: str, made: list[str], declined: list[tuple[str, str]], renames: dict[str, str]) -> None:
    """What an Organize did besides moving files: the folders it made, the noes and the renames."""
    st = _batches()
    st[batch] = {"made": made, "declined": declined, "renamed": renames}
    config.BATCHES.parent.mkdir(parents=True, exist_ok=True)
    config.BATCHES.write_text(json.dumps(st, indent=1))


def forget(batch: str) -> list[str]:
    """Take back an Organize's noes and renames; returns the folders it made, for the caller to
    remove when they are empty again."""
    st = _batches()
    b = st.pop(batch, None)
    if b is None:
        return []
    no = rejected()
    for path, folder in b["declined"]:
        no.get(path, {}).pop(folder, None)
    config.REJECTED.write_text(json.dumps({p: fs for p, fs in no.items() if fs}, indent=1))
    names = renamed()
    for proposed, folder in b["renamed"].items():
        if names.get(proposed) == folder:
            del names[proposed]
    config.RENAMED.write_text(json.dumps(names, indent=1))
    config.BATCHES.write_text(json.dumps(st, indent=1))
    return b["made"]


def returned() -> set[str]:
    """Paths an undo put a file back at."""
    return {m["from"] for m in log() if m.get("undone")}


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
    """The move undo() would put back: the one that put a file at moved_to, else the newest move of
    any kind (ariadne's, yours, or the last of an Organize batch)."""
    for m in reversed(log()):
        if not m.get("undone") and (moved_to is None or m["to"] == moved_to):
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
