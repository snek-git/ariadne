"""SQLite on disk, numpy in memory. Each file has a vector (its name and content blended) and
parts (name, text chunks, picture) with their own vectors; search scores a file by its best part."""
import sqlite3
import threading
from pathlib import Path

import numpy as np

from . import config
from .embed import DIM

SCHEMA = """
create table if not exists files (id integer primary key, path text unique not null, size integer,
  mtime real, kind text, vec blob);
create table if not exists parts (file_id integer not null references files(id) on delete cascade,
  idx integer, kind text, text text, vec blob);
create index if not exists parts_file on parts(file_id);
"""


def blob(v: np.ndarray) -> bytes:
    return v.astype(np.float16).tobytes()


def unblob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float16)


class Store:
    def __init__(self, path: Path = config.DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("pragma foreign_keys = on")
        self.db.execute("pragma journal_mode = wal")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self._mem = None

    def known(self) -> dict[str, tuple[int, float]]:
        with self.lock:
            return {p: (s, m) for p, s, m in self.db.execute("select path, size, mtime from files")}

    def put(self, path: str, size: int, mtime: float, kind: str, parts: list[tuple[str, str | None, np.ndarray]],
            vec: np.ndarray) -> None:
        with self.lock, self.db:
            self.db.execute("delete from files where path = ?", (path,))
            fid = self.db.execute("insert into files (path, size, mtime, kind, vec) values (?, ?, ?, ?, ?)",
                                  (path, size, mtime, kind, blob(vec))).lastrowid
            self.db.executemany("insert into parts values (?, ?, ?, ?, ?)",
                                [(fid, i, k, t, blob(v)) for i, (k, t, v) in enumerate(parts)])
            self._mem = None

    def remove(self, path: str) -> None:
        with self.lock, self.db:
            under = path.rstrip("/").replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "/%"
            self.db.execute(r"delete from files where path = ? or path like ? escape '\'", (path, under))
            self._mem = None

    def rename(self, old: str, new: str) -> None:
        """A moved file or folder keeps its vectors: nothing to re-embed. Renaming something the
        index does not hold is a no-op (the watcher also reports moves ariadne made itself)."""
        pre = old.rstrip("/") + "/"
        under = pre.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
        with self.lock, self.db:
            if self.db.execute("select 1 from files where path = ?", (old,)).fetchone():
                self.db.execute("delete from files where path = ?", (new,))
                self.db.execute("update files set path = ? where path = ?", (new, old))
            self.db.execute(r"update files set path = ? || substr(path, ?) where path like ? escape '\'",
                            (new.rstrip("/") + "/", len(pre) + 1, under))
            self._mem = None

    def mem(self) -> dict:
        """Everything search needs, as arrays; rebuilt after a write."""
        with self.lock:
            if self._mem is None:
                rows = self.db.execute("select id, path, kind, vec from files order by id").fetchall()
                ids = np.array([r[0] for r in rows], dtype=np.int64)
                fvecs = np.stack([unblob(r[3]) for r in rows]).astype(np.float32) if rows else np.zeros((0, DIM), np.float32)
                prow = self.db.execute("select file_id, idx, kind, vec from parts").fetchall()
                pvecs = np.stack([unblob(r[3]) for r in prow]).astype(np.float32) if prow else np.zeros((0, DIM), np.float32)
                self._mem = {
                    "ids": ids, "paths": [r[1] for r in rows], "kinds": [r[2] for r in rows], "vecs": fvecs,
                    "row": {int(i): n for n, i in enumerate(ids)},
                    "p_file": np.array([r[0] for r in prow], dtype=np.int64),
                    "p_idx": np.array([r[1] for r in prow], dtype=np.int64),
                    "p_kind": [r[2] for r in prow], "p_vecs": pvecs,
                }
                kinds = np.array(self._mem["p_kind"])
                self._mem["p_groups"] = {k: np.flatnonzero(kinds == k) for k in set(self._mem["p_kind"])}
            return self._mem

    def part_text(self, fid: int, idx: int) -> str | None:
        with self.lock:
            r = self.db.execute("select text from parts where file_id = ? and idx = ?", (fid, idx)).fetchone()
            return r[0] if r else None

    def first_text(self, path: str) -> str | None:
        """The file's first text chunk, or None for pictures and name-only files."""
        with self.lock:
            r = self.db.execute("select p.text from parts p join files f on f.id = p.file_id "
                                "where f.path = ? and p.idx = 1 and p.kind = 'chunk'", (path,)).fetchone()
            return r[0] if r else None

    def sized(self, size: int) -> list[str]:
        """Indexed files of exactly this many bytes: the only ones that can be copies."""
        with self.lock:
            return [r[0] for r in self.db.execute("select path from files where size = ?", (size,))]

    def file_vec(self, path: str) -> np.ndarray | None:
        with self.lock:
            r = self.db.execute("select vec from files where path = ?", (path,)).fetchone()
            return unblob(r[0]).astype(np.float32) if r else None

    def count(self) -> dict:
        with self.lock:
            return dict(self.db.execute("select kind, count(*) from files group by kind").fetchall())
