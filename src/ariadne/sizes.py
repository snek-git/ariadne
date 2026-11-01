"""Disk use per folder, for the map: blocks really used on disk, symlinks skipped. A walk is cached
for a minute and runs off the request thread; a slow one answers `pending` and bumps the version
when it lands."""
import os
import stat
import threading
import time
from pathlib import Path

TTL = 60
WAIT = 1.0  # seconds a request waits for a walk before answering pending


def walk(under: str) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Total bytes on disk below each folder, and each folder's subfolders."""
    total: dict[str, int] = {}
    kids: dict[str, list[str]] = {}
    for d, dirs, files in os.walk(under, topdown=False):
        size = 0
        for f in files:
            try:
                st = os.lstat(os.path.join(d, f))
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                size += st.st_blocks * 512
        subs = [os.path.join(d, x) for x in dirs if os.path.join(d, x) in total]  # symlinked dirs are never walked
        try:
            size += os.lstat(d).st_blocks * 512
        except OSError:
            pass
        total[d] = size + sum(total[s] for s in subs)
        kids[d] = subs
    return total, kids


def tree(path: str, total: dict, kids: dict, depth: int) -> dict:
    node = {"path": path, "name": Path(path).name, "size": total.get(path, 0), "kids": []}
    if depth > 0:
        node["kids"] = sorted((tree(k, total, kids, depth - 1) for k in kids.get(path, [])), key=lambda n: -n["size"])
    return node


class Sizes:
    def __init__(self, bump):
        self.bump = bump
        self.cache: dict[str, tuple[float, dict, dict]] = {}  # folder -> (when, total, kids)
        self.jobs: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def _run(self, under: str) -> None:
        total, kids = walk(under)
        with self.lock:
            self.cache[under] = (time.time(), total, kids)
            self.jobs.pop(under, None)
        self.bump()

    def _fresh(self, under: str) -> tuple | None:
        with self.lock:
            hit = self.cache.get(under)
        return hit if hit and time.time() - hit[0] < TTL else None

    def get(self, under: str, depth: int = 3) -> dict:
        under = os.path.normpath(under)
        if not os.path.isdir(under) or os.path.islink(under):
            raise ValueError(f"not a folder: {under}")
        hit = self._fresh(under)
        if hit is None:
            with self.lock:
                job = self.jobs.get(under)
                if job is None:
                    job = self.jobs[under] = threading.Thread(target=self._run, args=(under,), daemon=True)
                    job.start()
            job.join(WAIT)
            hit = self._fresh(under)
        if hit is None:
            return {"path": under, "name": Path(under).name, "size": None, "kids": [], "pending": True}
        return tree(under, hit[1], hit[2], depth)
