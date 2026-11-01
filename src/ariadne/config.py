"""What gets indexed and how. One place to change the policy."""
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".local/share/ariadne"
DB = DATA / "index.db"
MOVES = DATA / "moves.jsonl"
TIDY = DATA / "tidy.json"  # skipped proposals and muted folders
DESCRIPTIONS = HOME / ".config/ariadne/folders.txt"  # your own folder descriptions for Jev
TRASH = HOME / ".local/share/Trash"  # the freedesktop trash `gio trash` uses
HOST, PORT = "127.0.0.1", 8199

ROOTS = [HOME / "Documents", HOME / "Downloads", HOME / "Pictures", HOME / "Desktop", HOME / "Videos"]
# Top level only. Files sitting loose here are not used as routing examples, so nothing gets
# routed back into a drop zone.
DROP_ZONES = [HOME / "Downloads", HOME / "Downloads/Telegram Desktop", HOME / "Desktop"]

# Skipped whole: hidden dirs, dependency and build trees, app-owned folders, the private picture folder.
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", ".venv", "venv", "target", "dist", "build"}
SKIP_PATHS = {HOME / "Documents/Claude", HOME / "Documents/Codex", HOME / "Pictures/h"}

# Skipped files: source code, compiled objects, game containers, secrets.
SKIP_EXT = {
    "py", "pyc", "pyi", "pyo", "ts", "tsx", "js", "mjs", "cjs", "mts", "cts", "map", "jsx",
    "c", "cc", "cpp", "h", "hh", "hpp", "f", "f90", "rs", "go", "java", "o", "so", "dll", "a", "lib",
    "pak", "ucas", "utoc", "sample", "pb", "pem", "key", "part", "tmp", "crdownload", "lock",
}

# Content embedded in full (chunked). Data files (csv, json, log) are name-only: one 224 MB CSV
# would be 64k chunks of numbers that say nothing a filename doesn't.
TEXT_EXT = {"md", "txt", "rst", "org", "tex", "html", "htm", "srt"}
PDF_EXT = {"pdf"}
IMAGE_EXT = {"png", "jpg", "jpeg", "webp", "gif", "bmp"}

CHUNK = 3500  # characters, about 1k tokens
OVERLAP = 300
