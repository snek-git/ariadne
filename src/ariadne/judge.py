"""The judge step for Tidy: Opus, through `claude -p`, reads one idea the rules found and says
whether a careful person would make that change. The rules stay cheap and local and make the
candidates; only what Opus says yes to is shown. It sees the paths, the names in the folders
involved and the first ~1k characters of text where a file has some, so those leave the machine.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config
from .extract import kind, read_text

MODEL = "opus"
NAMES = 40  # file names shown per folder, with the total
EXCERPT = 1000  # characters of text per file
EXCERPTS = 3  # files read from one idea
TIMEOUT = 180
SCHEMA = json.dumps({
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["ok", "reason"],
    "additionalProperties": False,
})
SYSTEM = """You judge tidy ideas for one person's own files on their Linux home. A local index found
each idea by rule: a file that seems to sit in the wrong project, a second copy of a file, or two
folders that seem to hold the same thing. Rules make mistakes: a project's own layout can look
like clutter, notes link pictures by name, numbered variants are not copies.

Say ok only when you are sure the person would want the change made as written, from what you
see. When it could break something or you are unsure, say no. The reason is one short line,
at most 12 words, no advice."""


def available() -> bool:
    return shutil.which("claude") is not None


def short(p: str) -> str:
    return p.replace(str(config.HOME), "~", 1)


def listing(folder: str) -> str:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return f"{short(folder)}: (cannot list)"
    shown = ", ".join(names[:NAMES])
    more = f", and {len(names) - NAMES} more" if len(names) > NAMES else ""
    return f"{short(folder)} ({len(names)} entries): {shown}{more}"


def excerpt(p: str) -> str | None:
    """The first characters of a text file, or of the first text file in a folder."""
    q = Path(p)
    if q.is_dir():
        q = next((f for f in sorted(q.iterdir()) if f.is_file() and kind(f) == "text"), None)
        if q is None:
            return None
    elif kind(q) != "text":
        return None
    try:
        s = " ".join(read_text(q)[:EXCERPT * 2].split())[:EXCERPT]
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{short(str(q))}:\n{s}" if s else None


def prompt(p: dict) -> str:
    """The idea in words, then what is around it."""
    if p["kind"] == "misfiled":
        names = ", ".join(Path(f).name for f in p["paths"])
        what = f"Move {len(p['paths'])} file(s) ({names}) from {short(p['from'])} into {short(p['to'])}."
        folders = [p["from"], p["to"]]
    elif p["kind"] == "duplicate":
        keep, extra = p["paths"]
        what = f"{short(extra)} has the same bytes as {short(keep)}. Trash {short(extra)} and keep {short(keep)}."
        folders = list(dict.fromkeys(str(Path(x).parent) for x in p["paths"]))
    else:
        src, dst = p["paths"]
        what = f"Merge the folder {short(src)} into {short(dst)}: move what it holds there."
        folders = [src, dst]
    out = [f"Idea ({p['kind']}): {what}", "", "Folders:"] + [listing(f) for f in folders]
    texts = [e for e in map(excerpt, p["paths"][:1] if p["kind"] == "duplicate" else p["paths"][:EXCERPTS]) if e]  # copies read the same
    if texts:
        out += ["", "Text:"] + texts
    return "\n".join(out)


def ask(p: dict) -> dict | None:
    """Opus's verdict on one idea, {"ok", "reason"}; None when claude is missing or fails."""
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", MODEL, "--safe-mode", "--tools", "", "--strict-mcp-config",
             "--no-session-persistence", "--output-format", "json", "--system-prompt", SYSTEM,
             "--json-schema", SCHEMA, prompt(p)],
            capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL, cwd=config.DATA,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"judge {p['id']}: {e}", flush=True)
        return None
    try:
        out = json.loads(r.stdout)
        if r.returncode or out.get("is_error"):
            raise RuntimeError(out.get("result"))
        v = out["structured_output"]
        return {"ok": bool(v["ok"]), "reason": str(v["reason"]).strip()}
    except (ValueError, KeyError, TypeError, RuntimeError) as e:
        print(f"judge {p['id']}: exit {r.returncode}: {e}\n{r.stderr}", flush=True)
        return None
