"""Search, look-alikes, near-duplicates and folder routing over the in-memory index."""
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import config
from .store import Store

K = 10
MAJORITY = 0.6


def _under(paths: list[str], under: str | None) -> np.ndarray:
    if not under:
        return np.ones(len(paths), dtype=bool)
    pre = under.rstrip("/") + "/"
    return np.array([p.startswith(pre) for p in paths], dtype=bool)


def search(store: Store, q: np.ndarray, n: int = 20, under: str | None = None) -> list[dict]:
    """Files ranked by their best-matching part (name, a text chunk or the picture). A text query
    scores lower against pictures than against text (the modality gap), so each kind of part is
    ranked by how far it stands above the rest of its kind: a z-score per kind."""
    m = store.mem()
    if not len(m["p_vecs"]):
        return []
    raw = m["p_vecs"] @ q
    s = np.empty_like(raw)
    for kind, idx in m["p_groups"].items():
        s[idx] = (raw[idx] - raw[idx].mean()) / max(float(raw[idx].std()), 1e-6)
    ok = _under(m["paths"], under)
    out, seen = [], set()
    for i in np.argsort(-s):
        fid = int(m["p_file"][i])
        if fid in seen:
            continue
        r = m["row"][fid]
        if not ok[r]:
            continue
        seen.add(fid)
        kind = m["p_kind"][i]
        snippet = store.part_text(fid, int(m["p_idx"][i])) if kind == "chunk" else None
        out.append({"path": m["paths"][r], "score": round(float(s[i]), 3), "similarity": round(float(raw[i]), 4),
                    "match": kind, "snippet": snippet})
        if len(out) >= n:
            break
    return out


def similar(store: Store, path: str, n: int = 12) -> list[dict]:
    m = store.mem()
    v = store.file_vec(path)
    if v is None:
        return []
    s = m["vecs"] @ v
    out = []
    for i in np.argsort(-s):
        if m["paths"][i] != path:
            out.append({"path": m["paths"][i], "score": round(float(s[i]), 4)})
        if len(out) >= n:
            break
    return out


def dupes(store: Store, threshold: float = 0.97, under: str | None = None) -> list[dict]:
    """Pairs of different files whose vectors nearly match: renamed copies, drafts with small edits."""
    m = store.mem()
    ok = np.flatnonzero(_under(m["paths"], under))
    vecs = m["vecs"][ok]
    out = []
    for a in range(0, len(ok), 2048):
        s = vecs[a:a + 2048] @ vecs.T
        for i, j in zip(*np.nonzero(s >= threshold)):
            gi, gj = a + i, j
            if gi < gj:
                out.append({"a": m["paths"][ok[gi]], "b": m["paths"][ok[gj]], "score": round(float(s[i, j]), 4)})
    return sorted(out, key=lambda d: -d["score"])


def labeled(paths: list[str]) -> np.ndarray:
    """Files that sit in a folder you made: not loose at the top of a drop zone or a root."""
    tops = {str(r) for r in config.ROOTS} | {str(d) for d in config.DROP_ZONES}
    return np.array([str(Path(p).parent) not in tops for p in paths], dtype=bool)


def ancestors(folder: str) -> list[str]:
    """The folder and its parents, stopping above the root it lives in (a root is never a target)."""
    p, out = Path(folder), []
    roots = set(config.ROOTS)
    while p not in roots and p != p.parent:
        out.append(str(p))
        p = p.parent
    return out if p in roots else [folder]


def vote(sims: np.ndarray, folders: list[str], k: int = K, majority: float = MAJORITY) -> list[dict]:
    """Similarity-weighted vote of the k nearest files. Each neighbor votes for its folder and every
    folder above it, so siblings add up in their parent. The answer is the deepest folder holding a
    majority; its share reads as a confidence (1.0 = every neighbor agrees). Then the runners-up:
    the exact folders, best first."""
    top = np.argsort(-sims)[:k]
    exact, tree = defaultdict(float), defaultdict(float)
    for i in top:
        w = max(float(sims[i]), 0.0)
        exact[folders[i]] += w
        for a in ancestors(folders[i]):
            tree[a] += w
    total = sum(exact.values()) or 1.0
    held = [f for f, v in tree.items() if v / total >= majority]
    best = max(held, key=lambda f: (len(Path(f).parts), tree[f])) if held else max(exact, key=exact.get)
    out = [{"folder": best, "share": round(tree[best] / total, 3)}]
    for f, v in sorted(exact.items(), key=lambda kv: -kv[1]):
        if f != best:
            out.append({"folder": f, "share": round(v / total, 3)})
    return out


def route(store: Store, v: np.ndarray, exclude: str | None = None, k: int = K) -> list[dict]:
    m = store.mem()
    mask = labeled(m["paths"])
    if exclude:
        mask &= np.array([p != exclude for p in m["paths"]], dtype=bool)
    idx = np.flatnonzero(mask)
    sims = m["vecs"][idx] @ v
    folders = [str(Path(m["paths"][i]).parent) for i in idx]
    return vote(sims, folders, k)
