"""Runtime settings, read once from the environment.

Everything Outpost keeps lives under one data directory (``OUTPOST_DATA``,
default ``./outpost-data``): the SQLite database, imported problems, and the
scratch boxes the judge runs code in. Back that directory up and you have backed
up the whole contest.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _default_workers() -> int:
    # Leave one core for the web server and the admin's own use; timings are
    # steadier when judges don't fight the browser for a CPU.
    return max(1, (os.cpu_count() or 2) - 1)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    workers: int
    # First uid handed to sandboxed runs; worker N uses base + N so two runs
    # never share a uid (and so never see each other's processes or files).
    sandbox_uid_base: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "outpost.db"

    @property
    def problems_dir(self) -> Path:
        return self.data_dir / "problems"

    @property
    def boxes_dir(self) -> Path:
        return self.data_dir / "boxes"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def secret_key(self) -> str:
        """Cookie-signing key, created on first use and kept in the data dir."""
        path = self.data_dir / "secret.key"
        if not path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_hex(32))
            path.chmod(0o600)
        return path.read_text().strip()

    def ensure_dirs(self) -> None:
        # Contestant code runs as an unprivileged uid; keeping everything we
        # write private (tests, answers, the database) is what stops a
        # submission from reading the answers.
        os.umask(0o077)
        for d in (self.data_dir, self.problems_dir, self.uploads_dir, self.boxes_dir):
            d.mkdir(parents=True, exist_ok=True)
        for d in (self.problems_dir, self.uploads_dir):
            d.chmod(0o700)
        # The sandbox uids must be able to walk down to their own box, so these
        # two are traversable but not listable; each box is owned by its run.
        self.data_dir.chmod(0o711)
        self.boxes_dir.chmod(0o711)
        if os.geteuid() == 0:
            for parent in self.data_dir.parents:
                if not parent.stat().st_mode & 0o001:
                    raise SystemExit(
                        f"{parent} is not traversable by other users (chmod o+x), so sandboxed "
                        f"runs can't reach their box under {self.boxes_dir}. Pick another "
                        f"OUTPOST_DATA or run: chmod o+x {parent}")


def load() -> Settings:
    data_dir = Path(os.environ.get("OUTPOST_DATA", "outpost-data")).resolve()
    return Settings(
        data_dir=data_dir,
        host=os.environ.get("OUTPOST_HOST", "0.0.0.0"),
        port=int(os.environ.get("OUTPOST_PORT", "8000")),
        workers=int(os.environ.get("OUTPOST_WORKERS", _default_workers())),
        sandbox_uid_base=int(os.environ.get("OUTPOST_SANDBOX_UID", "60000")),
    )
