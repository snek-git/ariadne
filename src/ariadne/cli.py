"""ariadne: search your files by meaning. `ariadne up` starts the daemon, everything else asks it."""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

URL = f"http://{config.HOST}:{config.PORT}"
ROOT = Path(__file__).resolve().parents[2]
UNIT = "ariadne"
DIM, BOLD, GOLD, OFF = "\033[2m", "\033[1m", "\033[33m", "\033[0m"


def call(endpoint: str, body: dict | None = None, **q):
    url = URL + endpoint + ("?" + urllib.parse.urlencode({k: v for k, v in q.items() if v is not None}) if q else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"ariadne: {json.load(e).get('error')}")
    except urllib.error.URLError:
        sys.exit("ariadne: daemon is not running (ariadne up)")


def short(p: str) -> str:
    return p.replace(str(Path.home()), "~", 1)


def bar(score: float, low: float, high: float) -> str:
    n = max(0, min(10, round((score - low) / (high - low) * 10)))
    return GOLD + "█" * n + DIM + "░" * (10 - n) + OFF


def show_hits(hits, snippets=False):
    """Search scores are z-scores (about 2 to 8), look-alike scores are cosine similarity."""
    low, high = (2.0, 8.0) if hits and "similarity" in hits[0] else (0.4, 1.0)
    for h in hits:
        tag = {"name": "name", "chunk": "text", "image": "picture"}.get(h.get("match"), "")
        print(f"{bar(h['score'], low, high)} {h['score']:.3f}  {short(h['path'])}  {DIM}{tag}{OFF}")
        if snippets and h.get("snippet"):
            print(DIM + h["snippet"] + OFF + "\n")


def show_decision(name: str, d: dict, route: list[dict]):
    """The decision first (vote or Jev), then the vote's ranking it came from."""
    from .decide import summary
    print(f"{BOLD}{name}{OFF} {summary(d).replace(str(Path.home()), '~')}")
    if d["by"] == "jev":
        for f, p in sorted(d["probabilities"].items(), key=lambda kv: -kv[1]):
            print(f"  {GOLD}{p:>5.0%}{OFF}  {short(f)}  {DIM}jev{OFF}")
        print(f"  {DIM}any of these fits: {d['fits']:.0%}{OFF}")
    else:
        for x in route:
            print(f"  {GOLD}{x['share']:>5.0%}{OFF}  {short(x['folder'])}  {DIM}vote{OFF}")


def main():
    ap = argparse.ArgumentParser(prog="ariadne", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the daemon in the foreground")
    sub.add_parser("up", help="start the daemon as a transient user unit")
    sub.add_parser("down", help="stop the daemon")
    sub.add_parser("status")
    sub.add_parser("index", help="embed new and changed files under the roots")
    f = sub.add_parser("find", help="search by meaning")
    f.add_argument("query", nargs="+")
    f.add_argument("-n", type=int, default=12)
    f.add_argument("--under", help="only files below this folder")
    f.add_argument("--snippets", action="store_true", help="print the matching text chunk in full")
    s = sub.add_parser("like", help="files like this one")
    s.add_argument("path")
    s.add_argument("-n", type=int, default=12)
    r = sub.add_parser("route", help="which of your folders a file belongs in")
    r.add_argument("path")
    d = sub.add_parser("dupes", help="near-duplicate files")
    d.add_argument("--under")
    d.add_argument("--threshold", type=float, default=0.97)
    sub.add_parser("suggestions", help="drop zone suggestions waiting for you")
    a = sub.add_parser("accept", help="move a dropped file where it was suggested (default: the newest)")
    a.add_argument("path", nargs="?")
    a.add_argument("--to", help="a different folder than the suggestion")
    sub.add_parser("undo", help="put the last move back (a whole Organize batch at once)")
    e = sub.add_parser("eval", help="holdout test of routing on your sorted folders")
    e.add_argument("--jev", action="store_true", help="also let Jev decide where the vote is unsure (calls the TypeSafe API)")
    o = ap.parse_args()

    if o.cmd == "serve":
        from .server import serve
        serve()
    elif o.cmd == "up":
        config.DATA.mkdir(parents=True, exist_ok=True)
        subprocess.run(["systemd-run", "--user", "--collect", f"--unit={UNIT}", f"--working-directory={ROOT}",
                        "-p", f"StandardOutput=append:{config.DATA / 'daemon.log'}",
                        "-p", f"StandardError=append:{config.DATA / 'daemon.log'}",
                        sys.executable, "-m", "ariadne.cli", "serve"], check=True)
        print(f"started; log: {short(str(config.DATA / 'daemon.log'))}")
    elif o.cmd == "down":
        subprocess.run(["systemctl", "--user", "stop", UNIT])
    elif o.cmd == "status":
        print(json.dumps(call("/status"), indent=2))
    elif o.cmd == "index":
        print(call("/index", {}))
    elif o.cmd == "find":
        q = " ".join(o.query)
        res = call("/search", q=q, n=o.n, under=os.path.abspath(o.under) if o.under else None)
        print(f"{BOLD}{q}{OFF}  {DIM}{res['took'] * 1000:.0f} ms{OFF}")
        show_hits(res["hits"], o.snippets)
    elif o.cmd == "like":
        p = os.path.abspath(o.path)
        print(f"{BOLD}like {short(p)}{OFF}")
        show_hits(call("/similar", path=p, n=o.n)["hits"])
    elif o.cmd == "route":
        p = os.path.abspath(o.path)
        res = call("/route", path=p)
        show_decision(Path(p).name, res["decision"], res["route"])
    elif o.cmd == "dupes":
        for x in call("/dupes", under=os.path.abspath(o.under) if o.under else None, threshold=o.threshold)["pairs"]:
            print(f"{x['score']:.3f}  {short(x['a'])}\n       {short(x['b'])}")
    elif o.cmd == "suggestions":
        for x in call("/suggestions")["suggestions"]:
            show_decision(Path(x["path"]).name, x["decision"], x["route"])
    elif o.cmd == "accept":
        path = os.path.abspath(o.path) if o.path else None
        if path is None:
            pending = call("/suggestions")["suggestions"]
            if not pending:
                sys.exit("ariadne: no suggestions waiting")
            path = pending[0]["path"]
        res = call("/accept", {"path": path, "folder": os.path.abspath(o.to) if o.to else None})
        print(f"moved {short(res['from'])} -> {short(res['to'])}  (ariadne undo puts it back)")
    elif o.cmd == "undo":
        res = call("/undo", {})
        if res["count"] == 1:
            print(f"put back {short(res['undone'][0]['from'])}")
        else:
            print(f"put back {res['count']} files" if res["count"] else "nothing to undo")
    elif o.cmd == "eval":
        from .evaluate import run, run_jev
        from .store import Store
        print(json.dumps(run_jev(Store()) if o.jev else run(Store()), indent=2))


if __name__ == "__main__":
    main()
