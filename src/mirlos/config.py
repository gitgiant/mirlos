from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"MIRLOS_{name}", default)


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(Path.home() / "Mirlos"))))
    # where request audio lands (inside the music library, so rekordbox finds it next to everything else)
    music_dir: Path = field(default_factory=lambda: Path(_env("MUSIC_DIR", str(Path.home() / "Music" / "mirlos"))))
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8080")))
    dj_pin: str = field(default_factory=lambda: _env("DJ_PIN", ""))
    max_duration: int = field(default_factory=lambda: int(_env("MAX_DURATION", "600")))
    workers: int = field(default_factory=lambda: int(_env("WORKERS", "2")))
    history_poll: int = field(default_factory=lambda: int(_env("HISTORY_POLL", "30")))
    xml_path: Path | None = None

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir = Path(self.music_dir)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        if self.xml_path is None:
            self.xml_path = Path(_env("XML_PATH", str(self.data_dir / "rekordbox.xml")))
        if not self.dj_pin:
            self.dj_pin = self._persisted("dj_pin.txt", lambda: f"{secrets.randbelow(10**6):06d}")

    @property
    def requests_dir(self) -> Path:
        return self.music_dir

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mirlos.db"

    @property
    def secret_key(self) -> str:
        return self._persisted("secret.key", lambda: secrets.token_urlsafe(32))

    def _persisted(self, name: str, make) -> str:
        p = self.data_dir / name
        if p.exists():
            return p.read_text().strip()
        v = make()
        p.write_text(v)
        return v
