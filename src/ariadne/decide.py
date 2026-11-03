"""The decide step: Jev, TypeSafe's decision model, picks the folder from the embedding shortlist.

The embedding vote runs first. When it is sure (share >= VOTE_SURE, right 97.5% of the time on the
holdout) its answer stands and nothing leaves the machine. Otherwise one Jev request asks two
questions over the same file: a Choice among the shortlisted folders, each described by what is in
it, and a Noul for whether any of them fits at all. Jev returns probabilities, so the Choice's
probability of its pick is the confidence and the Noul says when a new folder is the better answer.
"""
import os
import time
from collections import defaultdict
from pathlib import Path

from typesafe_sdk import Choice, Noul, RetryPolicy, TypeSafeClient, TypeSafeError

from . import config
from .extract import ext, family, generic, name_text

SHORTLIST = 5
VOTE_SURE = 0.9
SAMPLES = 8  # file names shown per folder description
NONE_FITS = 0.3  # Noul below this: no shortlisted folder fits
# Auto-move only above these. Holdout: vote >= 0.9 was right 97.5% of the time (69% of files);
# Jev >= 0.7 was right 89% of the time on the files the vote was unsure of.
AUTO_JEV = 0.7
MEMO = 3600  # seconds Jev's answer for a file over one shortlist is reused
AUTO_FITS = 0.7
# A name-only file (an archive, say) is never sure when its nearest filed file is weaker than this.
# Leave-one-out over 1947 name-only files: every sure move below 0.70 was wrong; 0.75 drops 7 wrong
# sure moves for 4 right ones.
NAME_FLOOR = 0.75
TYPED = 0.8  # a folder sorted by type: this share of the files directly in it have one extension
KEY_FILE = Path.home() / ".config/typesafe/api_key"


def api_key() -> str | None:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    return key or None


def _entry(line: str) -> tuple[str, str] | None:
    """A `~/folder = what it holds` line as (absolute folder, text); None for comments and blanks."""
    if "=" not in line or line.lstrip().startswith("#"):
        return None
    folder, text = line.split("=", 1)
    return str(Path(folder.strip()).expanduser()), text.strip()


def overrides() -> dict[str, str]:
    """Descriptions you wrote yourself, one per line in config.DESCRIPTIONS: `~/folder = what it holds`."""
    lines = config.DESCRIPTIONS.read_text().splitlines() if config.DESCRIPTIONS.exists() else []
    return dict(e for e in map(_entry, lines) if e)


def set_override(folder: str, text: str) -> None:
    """Write your description of a folder in place of its old line; empty text removes it. Other
    lines and comments stay as they are."""
    folder, text = str(Path(folder).expanduser()), " ".join(text.split())
    home = str(Path.home())
    line = f"{'~' + folder[len(home):] if folder.startswith(home + '/') else folder} = {text}"
    old = config.DESCRIPTIONS.read_text().splitlines() if config.DESCRIPTIONS.exists() else []
    out, done = [], False
    for x in old:
        e = _entry(x)
        if e and e[0] == folder:
            if text and not done:
                out.append(line)
            done = True
        else:
            out.append(x)
    if text and not done:
        out.append(line)
    config.DESCRIPTIONS.parent.mkdir(parents=True, exist_ok=True)
    config.DESCRIPTIONS.write_text("".join(x + "\n" for x in out))


def members(paths: list[str]) -> dict[str, list[str]]:
    """Folder -> the names of the files directly in it."""
    out = defaultdict(list)
    for p in paths:
        out[str(Path(p).parent)].append(Path(p).name)
    return out


def describe(folder: str, names: list[str], subfolders: list[str], written: dict[str, str]) -> str:
    """What Jev sees for a folder: your description if you wrote one, else its path, size, a few
    file names and its subfolders. Built in code, so it is never stale."""
    rel = folder.replace(str(Path.home()), "~", 1)
    if folder in written:
        return f"{rel}: {written[folder]}"
    parts = [f"{rel}: {len(names)} files"]
    if names:
        parts.append("such as " + ", ".join(sorted(names)[:SAMPLES]))
    if subfolders:
        parts.append("subfolders " + ", ".join(sorted(subfolders)[:SAMPLES]))
    return "; ".join(parts)


class Decider:
    def __init__(self):
        key = api_key()
        if not key:
            raise RuntimeError(f"no TypeSafe key: set TYPESAFE_API_KEY or write it to {KEY_FILE}")
        self.client = TypeSafeClient(api_key=key, retry=RetryPolicy(max_retries=3, backoff_max=4.0, timeout=20.0))
        self.memo: dict[tuple, tuple[float, dict]] = {}  # (file, size, mtime, shortlist) -> (when, Jev's answer)

    def ask(self, file: dict, folders: list[str], names: dict[str, list[str]]) -> dict:
        """One Jev request: which shortlisted folder, and does any fit."""
        written = overrides()
        subs = defaultdict(list)
        for f in names:
            subs[str(Path(f).parent)].append(Path(f).name)
        keys = {f"f{i}": f for i, f in enumerate(folders)}
        criteria = {k: describe(f, names.get(f, []), subs.get(f, []), written) for k, f in keys.items()}
        r = self.client.system_one(
            state={"file": file},
            questions={
                "folder": Choice(instructions="Which of these folders should `file` be moved into?", criteria=criteria),
                "fits": Noul(instructions={
                    "question": "Does `file` belong in one of `folders`, as opposed to needing a new folder?",
                    "folders": list(criteria.values()),
                }),
            },
        )
        pick = r.choices["folder"]
        return {
            "folder": keys[pick.choice],
            "share": round(pick.probabilities[pick.choice], 3),
            "confidence": round(pick.confidence, 3),
            "fits": round(r.nouls["fits"].noul, 3),
            "by": "jev",
            "probabilities": {keys[k]: round(v, 3) for k, v in pick.probabilities.items()},
        }

    def decide(self, path: str, route: list[dict], text: str | None, names: dict[str, list[str]]) -> dict:
        """The vote's answer when it is sure or Jev could only see a name, else Jev's pick from the
        vote's shortlist. On the holdout Jev beat the vote on unsure text files (79% vs 76% on the
        right branch) and lost on name-only files (26% vs 68%) and pictures (37% vs 47%): with just a
        name, the vote's near-identical siblings know more than a folder description does."""
        top = route[0]
        if top["share"] >= VOTE_SURE or not text:
            return {"folder": top["folder"], "share": top["share"], "by": "vote"}
        p = Path(path)
        file = {"name": p.name, "readable name": name_text(p), "type": ext(p) or "none"}
        if p.exists():
            file["size"] = f"{p.stat().st_size / 1024:.0f} KB"
        if text:
            file["start of its text"] = text
        folders = [r["folder"] for r in route[:SHORTLIST]]
        # Jev answers a little differently each time. The same file over the same shortlist gets its
        # first answer again for MEMO seconds, so a Move and its Undo give back the same plan.
        st = p.stat() if p.exists() else None
        key = (path, st and st.st_size, st and st.st_mtime, tuple(folders))
        hit = self.memo.get(key)
        if hit and time.time() - hit[0] < MEMO:
            return hit[1]
        try:
            d = self.ask(file, folders, names)
            self.memo[key] = (time.time(), d)
            return d
        except TypeSafeError as err:  # offline or rate limited: the vote still answers
            print(f"jev failed for {p.name}: {err}", flush=True)
            return {"folder": top["folder"], "share": top["share"], "by": "vote", "jev_error": str(err)}


def grounds(path: str, folder: str, filed: list[str]) -> dict:
    """What the file's name and type say about the picked folder: is the name generic, does anything
    filed under the folder share the type family, and is it a folder sorted by type (most files
    directly in it share one family)."""
    e = family(Path(path))
    inside = [p for p in filed if p.startswith(folder + "/")]
    direct = [p for p in inside if str(Path(p).parent) == folder]
    return {"generic": generic(Path(path)), "held": any(family(Path(p)) == e for p in inside),
            "typed": bool(direct) and sum(family(Path(p)) == e for p in direct) / len(direct) >= TYPED}


def groundless(d: dict) -> str | None:
    """Why the picked folder is no real guess, or None. Leave-one-out: no sure move into a folder
    without the file's type was right (5 wrong, 0 right), and a generic name only finds files whose
    names are just as empty ('1.stl' voted with 'piece_001.stl' in ten unrelated subfolders)."""
    if not d.get("held", True):
        return "no folder like it holds this type of file"
    if d.get("named") and d.get("generic") and not d.get("typed"):
        return "its name says nothing about it"
    return None


def confident(d: dict) -> bool:
    """Sure enough to move the file without asking (you still get an Undo button)."""
    if not Path(d["folder"]).is_dir() or groundless(d):
        return False
    if d.get("named") and d.get("top", 1.0) < NAME_FLOOR:
        return False
    if d["by"] == "vote":
        return d["share"] >= VOTE_SURE
    return d["share"] >= AUTO_JEV and d["fits"] >= AUTO_FITS


def summary(d: dict) -> str:
    home = str(Path.home())
    where = d["folder"].replace(home, "~")
    if d.get("fits", 1.0) < NONE_FITS:
        return f"no folder fits well (closest: {where})"
    return f"belongs in {where}  ({d['share']:.0%} sure, by {d['by']})"
