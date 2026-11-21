"""Background services: download workers, rekordbox history poller, Funnel control."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from typing import Protocol

from . import rbxml
from .config import Config
from .store import Store

log = logging.getLogger("mirlos")


class Fetcher(Protocol):
    def search(self, query: str, n: int = 5) -> list: ...
    def resolve(self, video_id: str): ...
    def download(self, video_id: str, dest: Path) -> Path: ...


class Library:
    """Owns the XML file; call refresh() after any change that affects ready tracks."""

    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self._lock = threading.Lock()

    def refresh(self) -> None:
        with self._lock:
            rbxml.write(self.cfg.xml_path, self.store.ready("arrival"), self.store.ready("votes"))


class Downloader:
    def __init__(self, cfg: Config, store: Store, lib: Library, fetcher: Fetcher):
        self.cfg, self.store, self.lib, self.fetcher = cfg, store, lib, fetcher
        self.q: queue.Queue[int] = queue.Queue()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for r in self.store.pending():  # resume after restart
            self.q.put(r.id)
        for i in range(self.cfg.workers):
            t = threading.Thread(target=self._run, name=f"dl-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def enqueue(self, rid: int) -> None:
        self.q.put(rid)

    def _run(self) -> None:
        while True:
            self.process(self.q.get())

    def process(self, rid: int) -> None:
        r = self.store.get_request(rid)
        if not r or r.status not in ("queued", "downloading"):
            return
        self.store.set_status(rid, "downloading")
        try:
            path = self.fetcher.download(r.video_id, self.cfg.requests_dir)
        except Exception as e:
            log.warning("download %s failed: %s", r.video_id, e)
            self.store.set_status(rid, "failed", error=str(e)[:300])
            return
        cur = self.store.get_request(rid)
        if cur and cur.status == "downloading":  # may have been deleted meanwhile
            self.store.set_status(rid, "ready", path=str(path))
            self.lib.refresh()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.replace("\\", "/")))


def history_paths() -> set[str]:
    """Normalized file paths of every track in rekordbox's play history (read-only)."""
    try:
        from pyrekordbox import MasterDatabase as DB  # pyrekordbox >= 0.5
    except ImportError:
        from pyrekordbox import Rekordbox6Database as DB
    db = DB()
    try:
        return {_norm(s.Content.FolderPath) for s in db.get_history_songs()
                if s.Content is not None and s.Content.FolderPath}
    finally:
        db.close()


class HistoryPoller:
    def __init__(self, cfg: Config, store: Store, lib: Library, source=history_paths):
        self.cfg, self.store, self.lib, self.source = cfg, store, lib, source
        self.status = "starting"
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="history", daemon=True).start()

    def poll_once(self) -> int:
        ready = {_norm(r.path): r.id for r in self.store.ready() if r.path}
        if not ready:
            return 0
        played = self.source()
        n = 0
        for p, rid in ready.items():
            if p in played:
                self.store.set_status(rid, "played")
                n += 1
        if n:
            self.lib.refresh()
        return n

    def _run(self) -> None:
        fails = 0
        while not self._stop.wait(self.cfg.history_poll):
            try:
                self.poll_once()
                self.status, fails = "ok", 0
            except Exception as e:  # pyrekordbox missing / can't unlock / rekordbox busy
                fails += 1
                self.status = f"error: {e}"[:200]
                if fails in (1, 10):
                    log.warning("rekordbox history poll failed (%d): %s — use the manual 'played' button", fails, e)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets sent; picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Funnel:
    """Thin wrapper around the tailscale CLI."""

    def __init__(self, port: int):
        self.port = port
        self.exe = shutil.which("tailscale")

    def _run(self, *args: str) -> str:
        if not self.exe:
            raise RuntimeError("tailscale CLI not found on PATH")
        p = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=20)
        if p.returncode:
            raise RuntimeError((p.stderr or p.stdout).strip()[:300])
        return p.stdout

    def enable(self) -> None:
        self._run("funnel", "--bg", str(self.port))

    def disable(self) -> None:
        self._run("funnel", "--https=443", "off")

    def url(self) -> str | None:
        try:
            dns = json.loads(self._run("status", "--json"))["Self"]["DNSName"].rstrip(".")
            return f"https://{dns}"
        except Exception:
            return None
