"""SQLite state: guests, requests, votes, settings."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS guests (
  token TEXT PRIMARY KEY, name TEXT NOT NULL, banned INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL, uploader TEXT NOT NULL DEFAULT '', duration INTEGER, thumbnail TEXT,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued|downloading|ready|failed|played|deleted
  path TEXT, error TEXT, guest TEXT NOT NULL, created REAL NOT NULL, played_at REAL
);
CREATE TABLE IF NOT EXISTS votes (
  request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  guest TEXT NOT NULL, PRIMARY KEY (request_id, guest)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

VISIBLE = ("queued", "downloading", "ready", "played")


@dataclass
class Request:
    id: int
    video_id: str
    title: str
    uploader: str
    duration: int | None
    thumbnail: str | None
    status: str
    path: str | None
    error: str | None
    guest: str
    guest_name: str
    created: float
    played_at: float | None
    votes: int

    @property
    def duration_str(self) -> str:
        if not self.duration:
            return ""
        m, s = divmod(self.duration, 60)
        return f"{m}:{s:02d}"


class Store:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # settings
    def get(self, key: str, default: str = "") -> str:
        r = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return r[0]["value"] if r else default

    def set(self, key: str, value: str) -> None:
        self._q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))

    @property
    def paused(self) -> bool:
        return self.get("paused") == "1"

    # guests
    def guest(self, token: str) -> sqlite3.Row | None:
        r = self._q("SELECT * FROM guests WHERE token=?", (token,))
        return r[0] if r else None

    def upsert_guest(self, token: str, name: str) -> None:
        self._q("INSERT INTO guests(token,name,created) VALUES(?,?,?) "
                "ON CONFLICT(token) DO UPDATE SET name=excluded.name", (token, name, time.time()))

    def ban(self, token: str, banned: bool = True) -> None:
        self._q("UPDATE guests SET banned=? WHERE token=?", (int(banned), token))
        if banned:  # drop their not-yet-played requests
            self._q("UPDATE requests SET status='deleted' WHERE guest=? AND status IN ('queued','downloading','ready','failed')",
                    (token,))

    def guests(self) -> list[sqlite3.Row]:
        return self._q("SELECT g.*, (SELECT COUNT(*) FROM requests r WHERE r.guest=g.token) AS n "
                       "FROM guests g ORDER BY created")

    # requests
    _SELECT = ("SELECT r.*, COALESCE(g.name,'?') AS guest_name, "
               "(SELECT COUNT(*) FROM votes v WHERE v.request_id=r.id) AS votes "
               "FROM requests r LEFT JOIN guests g ON g.token=r.guest ")

    def _rows(self, where: str, args: tuple = (), order: str = "r.created") -> list[Request]:
        return [Request(**dict(r)) for r in self._q(self._SELECT + where + " ORDER BY " + order, args)]

    def by_video(self, video_id: str) -> Request | None:
        r = self._rows("WHERE r.video_id=?", (video_id,))
        return r[0] if r else None

    def get_request(self, rid: int) -> Request | None:
        r = self._rows("WHERE r.id=?", (rid,))
        return r[0] if r else None

    def submit(self, *, video_id: str, title: str, uploader: str, duration: int | None,
               thumbnail: str | None, guest: str) -> tuple[str, int]:
        """Add a request, or +1 an existing one. Returns (outcome, request_id)."""
        with self._lock:
            ex = self.by_video(video_id)
            if ex is None:
                cur = self._db.execute(
                    "INSERT INTO requests(video_id,title,uploader,duration,thumbnail,guest,created) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (video_id, title, uploader, duration, thumbnail, guest, time.time()))
                rid = cur.lastrowid
                self.vote(rid, guest)
                return "added", rid
            if ex.status == "deleted":
                return "blocked", ex.id
            if ex.status == "played":
                return "played", ex.id
            if ex.status == "failed":
                self._q("UPDATE requests SET status='queued', error=NULL WHERE id=?", (ex.id,))
                self.vote(ex.id, guest)
                return "retry", ex.id
            self.vote(ex.id, guest)
            return "voted", ex.id

    def vote(self, rid: int, guest: str) -> None:
        self._q("INSERT OR IGNORE INTO votes(request_id,guest) VALUES(?,?)", (rid, guest))

    def voted_by(self, guest: str) -> set[int]:
        return {r["request_id"] for r in self._q("SELECT request_id FROM votes WHERE guest=?", (guest,))}

    def set_status(self, rid: int, status: str, *, path: str | None = None, error: str | None = None) -> None:
        if status == "played":
            self._q("UPDATE requests SET status='played', played_at=? WHERE id=?", (time.time(), rid))
        elif path is not None:
            self._q("UPDATE requests SET status=?, path=?, error=NULL WHERE id=?", (status, path, rid))
        else:
            self._q("UPDATE requests SET status=?, error=? WHERE id=?", (status, error, rid))

    def pending(self) -> list[Request]:
        return self._rows("WHERE r.status IN ('queued','downloading')")

    def queue(self) -> list[Request]:
        """Public queue: unplayed in arrival order, then played (most recent first)."""
        live = self._rows("WHERE r.status IN ('queued','downloading','ready')")
        played = self._rows("WHERE r.status='played'", order="r.played_at DESC")
        return live + played

    def all(self) -> list[Request]:
        return self._rows("", order="r.created DESC")

    def ready(self, order: str = "arrival") -> list[Request]:
        o = "votes DESC, r.created" if order == "votes" else "r.created"
        return self._rows("WHERE r.status='ready' AND r.path IS NOT NULL", order=o)

    def reset_night(self) -> None:
        """Clear all requests and votes (downloaded files are kept)."""
        with self._lock:
            self._db.execute("DELETE FROM votes")
            self._db.execute("DELETE FROM requests")
