"""Online SQLite snapshots for nightly retention and owner download."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ctb.db.connection import Database, now_ms

__all__ = ["BACKUP_KEEP", "create_backup"]

BACKUP_KEEP: Final = 7


async def create_backup(
    db: Database,
    *,
    directory: str | Path | None = None,
    keep: int = BACKUP_KEEP,
    at: int | None = None,
) -> Path:
    """Create one consistent ``VACUUM INTO`` snapshot and retain the newest."""
    stamp = now_ms() if at is None else at
    target_dir = (
        Path(directory) if directory is not None else db.path.parent / "backups"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    label = datetime.fromtimestamp(stamp / 1000, tz=UTC).strftime("%Y%m%d-%H%M%S")
    destination = target_dir / f"ctb-{label}-{stamp % 1000:03d}.db"
    await db.vacuum_into(destination)

    snapshots = sorted(target_dir.glob("ctb-*.db"), reverse=True)
    for stale in snapshots[max(1, keep) :]:
        stale.unlink(missing_ok=True)
    return destination
