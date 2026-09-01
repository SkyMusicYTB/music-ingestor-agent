from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

logger = logging.getLogger(__name__)
_STAGING_NAME = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def cleanup_staging_directory(staging: Path | None) -> bool:
    """Remove only the known flat contents of one worker-created staging directory."""

    if staging is None:
        return True
    try:
        directory_stat = staging.lstat()
    except FileNotFoundError:
        return True
    if not stat.S_ISDIR(directory_stat.st_mode) or staging.is_symlink():
        return False
    clean = True
    with os.scandir(staging) as entries:
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(entry_stat.st_mode) or stat.S_ISLNK(entry_stat.st_mode):
                    os.unlink(entry.path)
                elif stat.S_ISDIR(entry_stat.st_mode):
                    # yt-dlp is configured for a flat staging directory. Do not
                    # recurse into an unexpected tree during automated cleanup.
                    os.rmdir(entry.path)
                else:
                    clean = False
            except OSError:
                clean = False
                logger.warning("could not clean staging entry %s", entry.name)
    try:
        staging.rmdir()
    except OSError:
        clean = False
        logger.warning("could not remove staging directory %s", staging.name)
    return clean


def cleanup_orphaned_staging(root: Path, *, preserve_job_ids: set[str] | None = None) -> int:
    """Remove only terminal/stale UUID directories not needed by resumable jobs."""

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("download root must be a real directory")
    removed = 0
    preserved = preserve_job_ids or set()
    with os.scandir(root) as entries:
        for entry in entries:
            if (
                entry.name in preserved
                or not _STAGING_NAME.fullmatch(entry.name)
                or not entry.is_dir(follow_symlinks=False)
            ):
                continue
            if cleanup_staging_directory(Path(entry.path)):
                removed += 1
    return removed
