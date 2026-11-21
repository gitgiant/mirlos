"""Background services: download workers, rekordbox history poller, Funnel control."""

from __future__ import annotations

import inspect
import json
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import analysis, rbxml
from .config import Config
from .store import Store

log = logging.getLogger("mirlos")


class Fetcher(Protocol):
    def search(self, query: str, n: int = 5) -> list: ...
    def resolve(self, video_id: str): ...
    def download(self, video_id: str, dest: Path) -> Path: ...
    def download_clip(self, video_id: str, dest: Path, duration: int | None = None) -> Path: ...


class Progress:
    """Live percent + start time for running downloads, keyed like ``req:12`` or ``full:<video id>``."""

    def __init__(self) -> None:
        self._d: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def track(self, key: str):
        """Start tracking ``key``; returns a ``progress(fraction, title)`` callback for the fetcher."""
        started = time.time()
        with self._lock:
            self._d[key] = (0, started)

        def cb(frac: float, _title: str = "") -> None:
            with self._lock:
                if key in self._d:
                    self._d[key] = (max(0, min(100, int(frac * 100))), started)
        return cb

    def done(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)

    def get(self, key: str) -> tuple[int, float] | None:
        with self._lock:
            return self._d.get(key)


def _download(fn, video_id: str, dest: Path, progress, **kw) -> Path:
    """Call a fetcher download function, passing ``progress`` only if it accepts one."""
    try:
        accepts = "progress" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        accepts = False
    return fn(video_id, dest, **kw, **({"progress": progress} if accepts else {}))


class Library:
    """Owns the XML file; call refresh() after any change that affects ready tracks."""

    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store
        self._lock = threading.Lock()

    def refresh(self) -> None:
        with self._lock:
            rbxml.write(self.cfg.xml_path, self.store.ready("arrival"), self.store.ready("votes"))


class Downloader:
    def __init__(self, cfg: Config, store: Store, lib: Library, fetcher: Fetcher, progress: Progress | None = None):
        self.cfg, self.store, self.lib, self.fetcher = cfg, store, lib, fetcher
        self.progress = progress or Progress()
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
        key = f"req:{rid}"
        try:
            path = _download(self.fetcher.download, r.video_id, self.cfg.requests_dir, self.progress.track(key))
        except Exception as e:
            log.warning("download %s failed: %s", r.video_id, e)
            self.store.set_status(rid, "failed", error=str(e)[:300])
            return
        finally:
            self.progress.done(key)
        cur = self.store.get_request(rid)
        if cur and cur.status == "downloading":  # may have been deleted meanwhile
            self.store.set_status(rid, "ready", path=str(path))
            self.lib.refresh()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.replace("\\", "/")))


@dataclass
class History:
    paths: set[str]  # normalized file paths of every track in rekordbox's play history
    last: dict | None  # most recent: title, artist, bpm, key


def read_history() -> History:
    """Read rekordbox's play history (read-only)."""
    try:
        from pyrekordbox import MasterDatabase as DB  # pyrekordbox >= 0.5
    except ImportError:
        from pyrekordbox import Rekordbox6Database as DB
    db = DB()
    try:
        paths, last, last_rank = set(), None, None
        for s in db.get_history_songs():
            c = s.Content
            if c is None:
                continue
            if c.FolderPath:
                paths.add(_norm(c.FolderPath))
            h = s.History
            rank = ((h.DateCreated or "") if h else "", s.TrackNo or 0)
            if last_rank is None or rank > last_rank:
                last_rank = rank
                last = {"title": c.Title or "", "artist": getattr(c.Artist, "Name", "") or "",
                        "bpm": (c.BPM or 0) / 100 or None,
                        "key": getattr(c.Key, "ScaleName", None)}
        return History(paths, last)
    finally:
        db.close()


class HistoryPoller:
    def __init__(self, cfg: Config, store: Store, lib: Library, source=read_history):
        self.cfg, self.store, self.lib, self.source = cfg, store, lib, source
        self.status = "starting"
        self.last: dict | None = None
        self.last_at: float = 0.0
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="history", daemon=True).start()

    def poll_once(self) -> int:
        h = self.source()
        if h.last != self.last:
            self.last = h.last
            self.last_at = time.time()
        n = 0
        for r in self.store.ready():
            if r.path and _norm(r.path) in h.paths:
                rid = r.id
                self.store.set_status(rid, "played")
                n += 1
        if n:
            self.lib.refresh()
        return n

    def _run(self) -> None:
        fails = 0
        while True:
            try:
                self.poll_once()
                self.status, fails = "ok", 0
            except Exception as e:  # pyrekordbox missing / can't unlock / rekordbox busy
                fails += 1
                self.status = f"error: {e}"[:200]
                if fails in (1, 10):
                    log.warning("rekordbox history poll failed (%d): %s — use the manual 'played' button", fails, e)
            if self._stop.wait(self.cfg.history_poll):
                return


class Target:
    """The BPM/key the DJ is playing at: a manual override, else the last played track."""

    def __init__(self, store: Store, hist: HistoryPoller):
        self.store, self.hist = store, hist

    def get(self) -> dict | None:
        bpm = self.store.get("target_bpm")
        if bpm:
            return {"bpm": float(bpm), "key": analysis.camelot(self.store.get("target_key")) or None,
                    "source": "override", "title": "", "at": 0.0}
        last = self.hist.last
        if last and last.get("bpm"):
            return {**last, "key": analysis.camelot(last.get("key")), "source": "last played",
                    "at": self.hist.last_at}
        return None

    def set(self, bpm: str, key: str) -> None:
        try:
            v = float(bpm)
        except ValueError:
            v = 0.0
        if not 40 <= v <= 250:
            raise ValueError("BPM must be 40–250")
        self.store.set("target_bpm", f"{v:.1f}")
        self.store.set("target_key", analysis.camelot(key) or "")

    def clear(self) -> None:
        self.store.set("target_bpm", "")
        self.store.set("target_key", "")


class Analyzer:
    """Downloads (clip or full) and analyzes BPM/key in the background.

    clip = guest search hints (45 s slice, approximate); full = DJ remix finder (whole
    track kept, so Add is instant). Guest jobs are bounded so a busy search box can't
    pile up downloads.
    """

    MAX_GUEST_BACKLOG = 15

    def __init__(self, cfg: Config, store: Store, fetcher: Fetcher, run=None, progress: Progress | None = None):
        self.cfg, self.store, self.fetcher = cfg, store, fetcher
        self.progress = progress or Progress()
        self.analyze = run or analysis.analyze
        self.dj: queue.Queue[str] = queue.Queue()
        self.guest: queue.Queue[str] = queue.Queue()
        self._cpu = threading.Lock()  # one analysis at a time; downloads overlap
        self.enabled = analysis.available() if run is None else True

    @property
    def clips_dir(self) -> Path:
        return self.cfg.data_dir / "clips"

    @property
    def cands_dir(self) -> Path:
        return self.cfg.data_dir / "candidates"

    def start(self) -> None:
        for kind in ("full", "clip"):
            for r in self.store._q("SELECT video_id FROM candidates WHERE kind=? AND status NOT IN ('done','failed')",
                                   (kind,)):
                (self.dj if kind == "full" else self.guest).put(r["video_id"])
        for name, q, kind in (("an-dj-0", self.dj, "full"), ("an-dj-1", self.dj, "full"),
                              ("an-guest", self.guest, "clip")):
            threading.Thread(target=self._run, args=(q, kind), name=name, daemon=True).start()

    def request(self, track, kind: str) -> None:
        if not self.enabled:
            return
        if kind == "clip" and (self.store.analysis(track.video_id) or self.guest.qsize() >= self.MAX_GUEST_BACKLOG):
            return
        if self.store.add_candidate(video_id=track.video_id, kind=kind, title=track.title,
                                    uploader=track.uploader, duration=track.duration,
                                    thumbnail=track.thumbnail):
            (self.dj if kind == "full" else self.guest).put(track.video_id)

    def _run(self, q: queue.Queue, kind: str) -> None:
        while True:
            self.process(q.get(), kind)

    def process(self, video_id: str, kind: str) -> None:
        c = self.store.candidate(video_id, kind)
        if not c or c["status"] in ("done", "failed"):
            return
        try:
            self.store.update_candidate(video_id, kind, status="downloading")
            key = f"{kind}:{video_id}"
            try:
                if kind == "full":
                    path = _download(self.fetcher.download, video_id, self.cands_dir, self.progress.track(key))
                else:
                    path = _download(self.fetcher.download_clip, video_id, self.clips_dir, self.progress.track(key),
                                     duration=c["duration"])
            finally:
                self.progress.done(key)
            self.store.update_candidate(video_id, kind, status="analyzing", path=str(path))
            with self._cpu:
                res = self.analyze(path, 90.0 if kind == "full" else 45.0)
            self.store.update_candidate(video_id, kind, status="done", bpm=res.bpm, key=res.key)
            if kind == "clip":
                Path(path).unlink(missing_ok=True)
                self.store.update_candidate(video_id, kind, path=None)
        except Exception as e:
            log.warning("analysis %s/%s failed: %s", kind, video_id, e)
            self.store.update_candidate(video_id, kind, status="failed", error=str(e)[:300])

    def purge(self, keep: set[str]) -> int:
        """Delete candidate/clip files not in ``keep`` (paths now used by requests)."""
        n = 0
        for p in self.store.drop_candidates():
            if _norm(p) not in keep:
                Path(p).unlink(missing_ok=True)
                n += 1
        for d in (self.clips_dir,):
            if d.exists():
                for f in d.iterdir():
                    f.unlink(missing_ok=True)
        return n


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packets sent; picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


_APPROVE_RE = re.compile(r"https://login\.tailscale\.com/[A-Za-z0-9/?=&._-]+")


class FunnelNeedsApproval(RuntimeError):
    """Funnel/HTTPS isn't enabled on the tailnet; the CLI waits on an admin-console link."""

    def __init__(self, link: str):
        super().__init__(f"Funnel isn't enabled on your tailnet yet. Approve it at {link}")
        self.link = link


class Funnel:
    """Thin wrapper around the tailscale CLI."""

    def __init__(self, port: int):
        self.port = port
        self.exe = shutil.which("tailscale")

    def _run(self, *args: str, timeout: float = 20) -> str:
        if not self.exe:
            raise RuntimeError("tailscale CLI not found on PATH")
        try:
            p = subprocess.run([self.exe, *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            # `tailscale funnel` prints an approval link and blocks until the admin approves it.
            out = "".join(x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
                          for x in (e.stdout, e.stderr))
            if m := _APPROVE_RE.search(out):
                raise FunnelNeedsApproval(m.group(0)) from None
            raise RuntimeError(f"tailscale {' '.join(args)} timed out after {timeout:g} s") from None
        if p.returncode:
            out = (p.stderr or p.stdout).strip()
            if m := _APPROVE_RE.search(out):
                raise FunnelNeedsApproval(m.group(0))
            raise RuntimeError(out[:300])
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
