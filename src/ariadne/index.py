"""Walk the roots, embed what is new or changed, drop what is gone."""
import os
import time
from pathlib import Path

import numpy as np

from . import config
from .embed import Embedder
from .extract import ext, kind, parts
from .store import Store

IMAGE_BATCH = 8
FLUSH_PARTS = 64


def skipped(p: Path) -> bool:
    return p in config.SKIP_PATHS or p.name.startswith(".") or p.name in config.SKIP_DIRS


def utf8(p: Path) -> bool:
    """Paths that are not valid UTF-8 cannot be stored in the index; they are skipped."""
    try:
        str(p).encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def wanted(p: Path) -> bool:
    return not p.name.startswith(".") and ext(p) not in config.SKIP_EXT and utf8(p)


def walk(roots=config.ROOTS):
    for root in roots:
        for d, dirs, files in os.walk(root):
            dp = Path(d)
            dirs[:] = [x for x in dirs if not skipped(dp / x)]
            for f in files:
                p = dp / f
                if wanted(p) and p.is_file():
                    yield p


def norm(v: np.ndarray) -> np.ndarray:
    return v / max(float(np.linalg.norm(v)), 1e-8)


def embed_files(emb: Embedder, files: list[Path]) -> list[tuple[Path, str, list, np.ndarray]]:
    """[(path, kind, [(part kind, text, vec)], file vec)] for files whose parts could be read."""
    got = [(p, parts(p)) for p in files]
    texts = [(fi, pi, t) for fi, (_, ps) in enumerate(got) for pi, (k, t) in enumerate(ps) if k != "image"]
    images = [(fi, pi, im) for fi, (_, ps) in enumerate(got) for pi, (k, im) in enumerate(ps) if k == "image"]
    vecs: dict[tuple[int, int], np.ndarray] = {}
    if texts:
        for (fi, pi, _), v in zip(texts, emb.texts([t for _, _, t in texts])):
            vecs[fi, pi] = v
    for i in range(0, len(images), IMAGE_BATCH):
        b = images[i:i + IMAGE_BATCH]
        for (fi, pi, _), v in zip(b, emb.images([im for _, _, im in b])):
            vecs[fi, pi] = v
    out = []
    for fi, (p, ps) in enumerate(got):
        rows = [(k, None if k == "image" else t, vecs[fi, pi]) for pi, (k, t) in enumerate(ps)]
        content = [v for k, _, v in rows if k != "name"]
        fvec = norm(rows[0][2] + norm(np.mean(content, axis=0))) if content else rows[0][2]
        out.append((p, kind(p), rows, fvec))
    return out


def index(store: Store, emb: Embedder, roots=config.ROOTS, log=print, left=lambda n: None) -> dict:
    """`left` hears how many files are still to embed, after the walk and after each batch."""
    known = store.known()
    seen, todo = set(), []
    for p in walk(roots):
        s = str(p)
        seen.add(s)
        st = p.stat()
        if known.get(s) != (st.st_size, st.st_mtime):
            todo.append(p)
    gone = [s for s in known if s not in seen and any(s.startswith(str(r) + "/") for r in roots)]
    for s in gone:
        store.remove(s)
    log(f"{len(seen)} files under the roots, {len(todo)} to embed, {len(gone)} gone")
    left(len(todo))
    t0, done, batch, nparts = time.time(), 0, [], 0
    for p in todo:
        batch.append(p)
        nparts += 1 if kind(p) != "text" else max(1, p.stat().st_size // config.CHUNK)
        if nparts >= FLUSH_PARTS or p is todo[-1]:
            for path, k, rows, fvec in embed_files(emb, batch):
                try:
                    st = path.stat()
                except OSError:  # gone while we embedded it
                    continue
                store.put(str(path), st.st_size, st.st_mtime, k, rows, fvec)
            done += len(batch)
            left(len(todo) - done)
            batch, nparts = [], 0
            dt = time.time() - t0
            log(f"{done}/{len(todo)} files, {done / dt:.1f}/s, {dt:.0f}s")
    return {"files": len(seen), "embedded": len(todo), "gone": len(gone)}
