"""Holdout test of routing: hide part of your sorted files, ask where each belongs, count how many
land back in the folder they came from. The folders you already made are the answer key."""
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import config
from .engine import labeled, vote
from .store import Store, unblob


def top_level(path: str) -> str:
    p = Path(path)
    for r in config.ROOTS:
        if p.is_relative_to(r):
            rel = p.relative_to(r).parts
            return f"{r.name}/{rel[0]}" if len(rel) > 1 else r.name
    return str(p.parent)


def name_vecs(store: Store, ids: np.ndarray) -> np.ndarray:
    rows = dict(store.db.execute("select file_id, vec from parts where idx = 0").fetchall())
    return np.stack([unblob(rows[int(i)]) for i in ids]).astype(np.float32)


def split(m: dict, holdout: float, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    L = np.flatnonzero(labeled(m["paths"]))
    rng = np.random.default_rng(seed)
    test = rng.choice(L, size=int(len(L) * holdout), replace=False)
    return L, test, np.setdiff1d(L, test)


def branch(truth: str, guess: str) -> bool:
    """Right folder, or a parent of it: the file lands on the right branch of the tree."""
    return truth == guess or truth.startswith(guess + "/")


def run(store: Store, holdout: float = 0.15, seed: int = 0, k: int = 10) -> dict:
    m = store.mem()
    L, test, train = split(m, holdout, seed)
    folder = [str(Path(p).parent) for p in m["paths"]]
    train_folders = {folder[i] for i in train}
    variants = {"blended": m["vecs"], "name only": name_vecs(store, m["ids"])}
    report = {"labeled": len(L), "train": len(train), "test": len(test),
              "folders": len({folder[i] for i in L}), "variants": {}}
    for name, V in variants.items():
        tf = [folder[i] for i in train]
        cents = defaultdict(list)
        for i in train:
            cents[folder[i]].append(V[i])
        cnames = list(cents)
        C = np.stack([np.mean(cents[c], axis=0) for c in cnames])
        C /= np.linalg.norm(C, axis=1, keepdims=True)
        hits = defaultdict(int)
        conf = []
        for i in test:
            truth = folder[i]
            sims = V[train] @ V[i]
            ranked = vote(sims, tf, k)
            guess = [r["folder"] for r in ranked]
            hits["knn top1"] += guess[0] == truth
            hits["knn right branch"] += branch(truth, guess[0])
            hits["knn top3"] += truth in guess[:3]
            hits["knn top-level"] += top_level(guess[0] + "/x") == top_level(truth + "/x")
            hits["centroid top1"] += cnames[int(np.argmax(C @ V[i]))] == truth
            hits["routable"] += truth in train_folders
            conf.append((ranked[0]["share"], branch(truth, guess[0])))
        n = len(test)
        res = {h: round(c / n, 3) for h, c in hits.items() if h != "routable"}
        res["routable share"] = round(hits["routable"] / n, 3)
        bands = {}
        for t in (0.5, 0.7, 0.9):
            sel = [ok for s, ok in conf if s >= t]
            bands[f"share>={t}"] = {"coverage": round(len(sel) / n, 3), "accuracy": round(sum(sel) / max(len(sel), 1), 3)}
        res["confidence"] = bands
        report["variants"][name] = res
    return report


def run_jev(store: Store, holdout: float = 0.15, seed: int = 0, k: int = 10, workers: int = 8) -> dict:
    """Same split as run(). The vote answers when it is sure; Jev decides the rest from the vote's
    shortlist, seeing only the training files in its folder descriptions."""
    from .decide import VOTE_SURE, Decider, members

    m = store.mem()
    _, test, train = split(m, holdout, seed)
    paths = m["paths"]
    folder = [str(Path(p).parent) for p in paths]
    names = members([paths[i] for i in train])
    tf = [folder[i] for i in train]
    text = dict(store.db.execute("select file_id, text from parts where idx = 1 and kind = 'chunk'").fetchall())
    decider = Decider()

    def one(i):
        ranked = vote(m["vecs"][train] @ m["vecs"][i], tf, k)
        d = decider.decide(paths[i], ranked, text.get(int(m["ids"][i])), names)
        shortlist = [r["folder"] for r in ranked[:5]]
        return folder[i], ranked[0], d, any(branch(folder[i], f) for f in shortlist)

    with ThreadPoolExecutor(workers) as pool:
        rows = list(pool.map(one, test))
    with (config.DATA / "eval-jev.jsonl").open("w") as f:  # every decision, for reading the misses
        for i, (t, v, d, s) in zip(test, rows):
            f.write(json.dumps({"path": paths[i], "kind": m["kinds"][i], "truth": t, "vote": v, "decision": d,
                                "in_shortlist": s}) + "\n")
    unsure = [r for r in rows if r[1]["share"] < VOTE_SURE]
    asked = [r for r in unsure if r[2]["by"] == "jev"]
    n = len(rows)
    rep = {
        "test": n,
        "vote sure (no Jev call)": len(rows) - len(unsure),
        "Jev calls": len(asked),
        "unsure but not readable (vote kept)": sum(1 for r in unsure if r[2]["by"] == "vote" and "jev_error" not in r[2]),
        "Jev errors": sum(1 for r in unsure if "jev_error" in r[2]),
        "all files": {
            "vote alone, exact": round(sum(t == v["folder"] for t, v, _, _ in rows) / n, 3),
            "vote alone, right branch": round(sum(branch(t, v["folder"]) for t, v, _, _ in rows) / n, 3),
            "vote + Jev, exact": round(sum(t == d["folder"] for t, _, d, _ in rows) / n, 3),
            "vote + Jev, right branch": round(sum(branch(t, d["folder"]) for t, _, d, _ in rows) / n, 3),
        },
    }
    if asked:
        a = len(asked)
        rep["files the vote was unsure of"] = {
            "vote, right branch": round(sum(branch(t, v["folder"]) for t, v, _, _ in asked) / a, 3),
            "Jev, right branch": round(sum(branch(t, d["folder"]) for t, _, d, _ in asked) / a, 3),
            "Jev, exact": round(sum(t == d["folder"] for t, _, d, _ in asked) / a, 3),
            "right answer was in the shortlist": round(sum(s for *_, s in asked) / a, 3),
        }
        bands = {}
        for t in (0.5, 0.7, 0.9):
            sel = [branch(tr, d["folder"]) for tr, _, d, _ in asked if d["share"] >= t]
            bands[f"Jev share>={t}"] = {"coverage": round(len(sel) / a, 3), "accuracy": round(sum(sel) / max(len(sel), 1), 3)}
        rep["Jev confidence"] = bands
        out = [d["fits"] for _, _, d, s in asked if not s]
        inn = [d["fits"] for _, _, d, s in asked if s]
        rep["Jev 'any folder fits' (mean)"] = {
            "when the right folder was in the shortlist": round(float(np.mean(inn)), 3) if inn else None,
            "when it was not": round(float(np.mean(out)), 3) if out else None,
        }
    return rep
