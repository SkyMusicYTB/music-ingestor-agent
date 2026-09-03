"""Read-only physical/index reconciliation; intended to run as music-agent."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Track
from app.services.library_metadata import LibraryReadError, read_audio_metadata
from app.services.library_presence import library_presence
from app.services.library_scan import ScanDiagnostics, iter_library_candidates


def audit_library(
    factory: sessionmaker[Session], music_root: Path, *, verbose: bool = False, limit: int = 100
) -> dict[str, object]:
    if not 1 <= limit <= 1000:
        raise ValueError("audit detail limit must be between 1 and 1000")
    root = music_root.resolve(strict=True)
    with factory() as session:
        known = {r.filepath: r.is_present for r in session.scalars(select(Track))}
    diagnostics = ScanDiagnostics()
    recognized_unindexed = present_but_absent = recognized = 0
    details: list[dict[str, str]] = []

    def detail(relative: str, reason: str) -> None:
        if verbose and len(details) < limit:
            details.append(
                {
                    "relative_path": "".join(c if c.isprintable() else "?" for c in relative)[:300],
                    "reason_code": reason,
                }
            )

    for path in iter_library_candidates(root, diagnostics):
        relative = path.relative_to(root).as_posix()
        try:
            metadata = read_audio_metadata(path, music_root=root)
        except (OSError, ValueError, TypeError) as error:
            reason = error.reason if isinstance(error, LibraryReadError) else "unreadable"
            diagnostics.issue(relative, reason)
            detail(relative, reason)
            continue
        recognized += 1
        codec = str(metadata.get("codec") or "unknown")
        diagnostics.by_codec[codec] = diagnostics.by_codec.get(codec, 0) + 1
        if metadata.get("_metadata_fallback"):
            diagnostics.counts["metadata_fallback"] += 1
        if not known.get(relative):
            recognized_unindexed += 1
            detail(relative, "recognized_unindexed")
    for relative, present in known.items():
        if present and library_presence(root, relative) == "missing":
            present_but_absent += 1
            detail(relative, "present_but_absent")
    result = diagnostics.payload(details=False)
    result.update(
        indexed_rows=len(known),
        present_rows=sum(known.values()),
        recognized_audio_files=recognized,
        recognized_unindexed=recognized_unindexed,
        present_but_absent=present_but_absent,
        samples=details,
    )
    return result
