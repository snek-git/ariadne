"""Watches every root, recursively. Moves and deletions update the index at once; new and changed
files are handed on once they have stopped growing, so the daemon can embed them and decide
whether to say anything."""
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from . import config
from .index import skipped, wanted

SETTLE = 2.0  # seconds a file must stay the same size before it counts as written


def visible(p: Path) -> bool:
    """Under a root, and neither p nor a folder above it is skipped (hidden ones, build trees)."""
    for root in config.ROOTS:
        if p.is_relative_to(root):
            here = root
            for part in p.relative_to(root).parts:
                here = here / part
                if skipped(here):
                    return False
            return True
    return False


def indexable(p: Path) -> bool:
    """Under a root, in no skipped folder, and a kind of file the index keeps."""
    return visible(p.parent) and wanted(p)


class Watcher(FileSystemEventHandler):
    """Callbacks are kept as new/changed/moved/gone: watchdog dispatches to on_moved and friends on
    this class, so the callbacks must not share those names."""

    def __init__(self, new, changed, moved, gone):
        self.new, self.changed, self.moved, self.gone = new, changed, moved, gone
        self.pending: dict[str, tuple[float, str]] = {}  # path -> (last event, "new" | "changed")
        self.lock = threading.Lock()

    def dispatch(self, event):
        try:
            super().dispatch(event)
        except Exception as err:  # one bad event must not stop the watcher thread
            print(f"watch: {event.event_type} {event.src_path} failed: {err!r}", flush=True)

    def _queue(self, path: str, kind: str) -> None:
        with self.lock:
            old = self.pending.get(path)
            self.pending[path] = (time.time(), "new" if old and old[1] == "new" else kind)

    def on_created(self, event):
        if not event.is_directory and indexable(Path(event.src_path)):
            self._queue(event.src_path, "new")

    def on_modified(self, event):
        if not event.is_directory and indexable(Path(event.src_path)):
            self._queue(event.src_path, "changed")

    def on_deleted(self, event):
        with self.lock:
            self.pending.pop(event.src_path, None)
        self.gone(event.src_path)

    def on_moved(self, event):
        src, dst = event.src_path, event.dest_path
        with self.lock:
            self.pending.pop(src, None)
        if event.is_directory:
            self.moved(src, dst)
        elif indexable(Path(dst)):
            self.moved(src, dst)  # keeps its vectors: a rename is not a change
            self._queue(dst, "new")  # browsers write .part files and rename them at the end
        else:
            self.gone(src)

    def settle(self) -> None:
        sizes: dict[str, int] = {}
        while True:
            time.sleep(0.5)
            with self.lock:
                ready = [(p, k) for p, (t, k) in self.pending.items() if time.time() - t > SETTLE]
            for p, kind in ready:
                try:
                    size = Path(p).stat().st_size
                except OSError:
                    with self.lock:
                        self.pending.pop(p, None)
                    continue
                if sizes.get(p) != size:
                    sizes[p] = size
                    with self.lock:
                        self.pending[p] = (time.time(), kind)
                    continue
                sizes.pop(p, None)
                with self.lock:
                    self.pending.pop(p, None)
                try:
                    (self.new if kind == "new" else self.changed)(p)
                except Exception as err:
                    print(f"watch: failed on {p}: {err!r}", flush=True)


def watch(on_new, on_changed, on_moved, on_gone) -> Observer:
    w = Watcher(on_new, on_changed, on_moved, on_gone)
    obs = Observer()
    for root in config.ROOTS:
        obs.schedule(w, str(root), recursive=True)
    obs.start()
    threading.Thread(target=w.settle, daemon=True).start()
    return obs
