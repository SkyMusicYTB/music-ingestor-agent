"""Bounded, read-only metadata reading for existing library files."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from mutagen import File as MutagenFile  # type: ignore[attr-defined]
from mutagen import MutagenError  # type: ignore[attr-defined]

from app.clients.ytdlp import minimal_subprocess_env
from app.services.duplicates import recording_version_signature
from app.services.library_formats import FORMATS, LibraryFormat, extension_for
from app.services.library_presence import open_library_file
from app.tags.provenance import PROVENANCE_TAG_FIELDS, provenance_snapshot
from app.workers.process import BoundedProcessError, run_bounded_process


class LibraryReadError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


SCANNED_PROVENANCE_FIELDS = (
    "source_provider",
    "source_uploader",
    "canonical_identity_verified",
    "metadata_authority",
    "metadata_provenance",
    "artists",
)


def _tag_text(value: object) -> str | None:
    if hasattr(value, "text"):
        return _tag_text(value.text)
    if hasattr(value, "value"):
        return _tag_text(value.value)
    if isinstance(value, bytes):
        value = value[:2048].decode("utf-8", "replace")
    if isinstance(value, list | tuple):
        if not value:
            return None
        if len(value) >= 2 and all(isinstance(v, int) for v in value[:2]):
            return f"{value[0]}/{value[1]}" if value[1] else str(value[0])
        return _tag_text(value[0])
    if value is None:
        return None
    value = " ".join(unicodedata.normalize("NFKC", str(value)[:2048]).split())
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    return value or None


def _first(tags: Any, *keys: str) -> str | None:
    if tags is None:
        return None
    for key in keys:
        try:
            value = tags.get(key)
        except (KeyError, ValueError):
            # Vorbis rejects non-ASCII ID3/MP4 key aliases; that is not a
            # malformed file and must not discard already decoded tags.
            continue
        result = _tag_text(value)
        if result:
            return result
    return None


def _artist_values(tags: Any, *keys: str) -> tuple[str, ...]:
    if tags is None:
        return ()
    for key in keys:
        try:
            value = tags.get(key)
        except (KeyError, ValueError):
            continue
        if hasattr(value, "text"):
            value = value.text
        items = value if isinstance(value, list | tuple) else (value,)
        result = tuple(
            dict.fromkeys(text[:300] for item in items[:16] if (text := _tag_text(item)))
        )
        if result:
            return result
    return ()


def _raw_tag(tags: Any, key: str, *aliases: str) -> str | None:
    if tags is None:
        return None
    keys = (key, *aliases)
    for name in keys:
        found = _first(tags, name, name.lower(), name.upper(), f"----:com.apple.iTunes:{name}")
        if found:
            return found
    if hasattr(tags, "getall"):
        for frame in tags.getall("TXXX"):
            if getattr(frame, "desc", "").casefold() in {name.casefold() for name in keys}:
                return _tag_text(frame)
        if key == "MUSICBRAINZ_TRACKID":
            for frame in tags.getall("UFID"):
                if getattr(frame, "owner", "") in {
                    "http://musicbrainz.org",
                    "https://musicbrainz.org",
                }:
                    return _tag_text(getattr(frame, "data", None))
    return None


def _number(value: str | None) -> tuple[int | None, int | None]:
    values = (value or "").split("/", 1)

    def number(raw: str) -> int | None:
        return int(raw) if raw.isdigit() and 0 < int(raw) <= 9999 else None

    return number(values[0]), number(values[1]) if len(values) > 1 else None


def _year(value: str | None) -> int | None:
    try:
        year = int((value or "")[:4])
    except ValueError:
        return None
    return year if 1000 <= year <= 2200 else None


def _positive(value: object) -> float | None:
    try:
        number = float(str(value))
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _mbid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(value)) if value else None
    except ValueError:
        return None


def filename_metadata(relative: Path) -> dict[str, object]:
    """Infer only an exact Artist/Album/[Disc NN/]file layout."""
    parts = relative.parts
    result: dict[str, object] = {"title": (_tag_text(relative.stem) or "Untitled")[:300]}
    match = re.fullmatch(r"(\d{1,3})\s+-\s+(.+)", str(result["title"]))
    if match:
        result.update(title=match[2][:300], track_number=int(match[1]))
    if len(parts) == 4 and re.fullmatch(r"Disc \d{1,3}", parts[-2], re.I):
        result["disc_number"] = int(parts[-2].split()[-1])
        parts = parts[:-2] + parts[-1:]
    if len(parts) == 3 and parts[0].casefold() not in {"music", "downloads", "various", "unknown"}:
        result["artist"] = (_tag_text(parts[0]) or "Unknown Artist")[:300]
        album = _tag_text(parts[1]) or ""
        year = re.fullmatch(r"(.+) \((\d{4})\)", album)
        result["album"] = (year[1] if year else album)[:300] or None
        if year:
            result["year"] = _year(year[2])
    return result


def _read_tags(raw: Any, easy: Any) -> dict[str, object]:
    fields = {
        "artist": ("artist", "TPE1", "\xa9ART", "Author"),
        "title": ("title", "TIT2", "\xa9nam", "Title"),
        "album": ("album", "TALB", "\xa9alb", "WM/AlbumTitle"),
        "album_artist": ("albumartist", "TPE2", "aART", "WM/AlbumArtist", "album_artist"),
        "genre": ("genre", "TCON", "\xa9gen", "WM/Genre"),
    }
    result: dict[str, object] = {}
    for field, keys in fields.items():
        value = _first(easy, *keys) or _first(raw, *keys)
        result[field] = value[: 200 if field == "genre" else 300] if value else None
    artists = _artist_values(easy, *fields["artist"]) or _artist_values(raw, *fields["artist"])
    if artists:
        result["artists"] = list(artists)
        result["artist"] = ", ".join(artists)[:300]
    result["year"] = _year(
        _first(easy, "date", "year")
        or _first(raw, "TDRC", "TYER", "\xa9day", "WM/Year", "date", "year")
    )
    result["track_number"], result["track_total"] = _number(
        _first(easy, "tracknumber", "track")
        or _first(raw, "TRCK", "trkn", "WM/TrackNumber", "tracknumber", "track")
    )
    result["disc_number"], result["disc_total"] = _number(
        _first(easy, "discnumber", "disc")
        or _first(raw, "TPOS", "disk", "WM/PartOfSet", "discnumber", "disc")
    )
    for field, keys in {
        "recording_mbid": (
            "MUSICBRAINZ_TRACKID",
            "MusicBrainz Track Id",
            "musicbrainz_recordingid",
        ),
        "release_mbid": ("MUSICBRAINZ_ALBUMID", "MusicBrainz Album Id"),
        "release_group_mbid": ("MUSICBRAINZ_RELEASEGROUPID", "MusicBrainz Release Group Id"),
    }.items():
        result[field] = _mbid(_raw_tag(raw, *keys))
    for field, cap in (
        ("source_extractor", 40),
        ("source_id", 100),
        ("source_url", 2048),
        ("job_id", 36),
    ):
        value = _raw_tag(raw, f"MUSIC_AGENT_{field.upper()}")
        result[field] = value[:cap] if value else None
    result.update(
        provenance_snapshot(
            {attribute: _raw_tag(raw, key) for key, attribute in PROVENANCE_TAG_FIELDS.items()}
        )
    )
    if result.get("canonical_identity_verified") is False:
        # Provider identity is not MusicBrainz identity, even if a copied source
        # carried stale MBID tags. Reading never promotes an unverified fallback.
        for field in ("recording_mbid", "release_mbid", "release_group_mbid"):
            result[field] = None
    return result


def _probe(file: BinaryIO, path: Path, policy: LibraryFormat) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if executable is None:
        raise LibraryReadError("probe_unavailable")
    input_path = str(path)
    inherited: tuple[int, ...] = ()
    if os.name == "posix":
        inherited = (file.fileno(),)
        input_path = (
            f"/proc/self/fd/{file.fileno()}"
            if Path("/proc/self/fd").exists()
            else f"/dev/fd/{file.fileno()}"
        )
    argv = [
        executable,
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        ",".join(policy.demuxers),
        "-probesize",
        "5242880",
        "-analyzeduration",
        "5000000",
        "-show_entries",
        "format=format_name,duration,bit_rate:format_tags:stream=codec_type,codec_name,duration,bit_rate:stream_tags:stream_disposition=attached_pic,default",
        "-of",
        "json",
        input_path,
    ]
    try:
        result = run_bounded_process(
            argv,
            environment=minimal_subprocess_env(),
            timeout_seconds=15,
            stdout_limit=512 * 1024,
            stderr_limit=64 * 1024,
            pass_fds=inherited,
        )
    except BoundedProcessError as error:
        raise LibraryReadError("probe_resource_limit") from error
    if result.returncode:
        raise LibraryReadError("malformed_audio")
    try:
        payload = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as error:
        raise LibraryReadError("malformed_probe") from error
    if not isinstance(payload, dict):
        raise LibraryReadError("malformed_probe")
    return payload


def probe_metadata(payload: dict[str, Any], policy: LibraryFormat) -> dict[str, object]:
    streams = payload.get("streams")
    if not isinstance(streams, list) or len(streams) > 64:
        raise LibraryReadError("malformed_probe")
    for stream in streams:
        if not isinstance(stream, dict):
            raise LibraryReadError("malformed_probe")
        if not isinstance(stream.get("disposition", {}), dict):
            raise LibraryReadError("malformed_probe")
        if stream.get("codec_type") == "video" and not (
            stream.get("disposition", {}).get("attached_pic") == 1
            and stream.get("codec_name") in {"mjpeg", "png"}
        ):
            raise LibraryReadError("video_bearing")
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    audio_count = len(audio)
    if audio_count > 1:
        audio = [s for s in audio if s.get("disposition", {}).get("default") == 1]
    if len(audio) != 1:
        raise LibraryReadError("no_audio" if audio_count == 0 else "ambiguous_audio_streams")
    selected = audio[0]
    codec = selected.get("codec_name")
    if codec not in policy.codecs:
        raise LibraryReadError("unsupported_codec")
    format_data = payload.get("format", {})
    if not isinstance(format_data, dict):
        raise LibraryReadError("malformed_probe")
    duration = _positive(selected.get("duration")) or _positive(format_data.get("duration"))
    if duration is None:
        raise LibraryReadError("missing_duration")
    tags: dict[str, Any] = {}
    for raw in (format_data.get("tags", {}), selected.get("tags", {})):
        if isinstance(raw, dict):
            tags.update({str(k).casefold(): v for k, v in raw.items()})
    result = _read_tags(tags, tags)
    bitrate = _positive(selected.get("bit_rate")) or _positive(format_data.get("bit_rate"))
    result.update(
        codec=codec,
        container=str(format_data.get("format_name") or policy.demuxers[0]).split(",")[0][:64],
        duration_seconds=duration,
        bitrate=int(bitrate) if bitrate else None,
    )
    return result


def validate_file_snapshot(
    root: Path, relative: str, metadata: dict[str, object]
) -> os.stat_result:
    """Revalidate a parsed file immediately before committing an index update."""
    try:
        with open_library_file(root, relative) as current:
            named = os.fstat(current.fileno())
    except (OSError, ValueError) as error:
        raise LibraryReadError("file_changed") from error
    for key, actual in (
        ("_file_device", named.st_dev),
        ("_file_inode", named.st_ino),
        ("_file_size", named.st_size),
        ("_file_mtime_ns", named.st_mtime_ns),
    ):
        if key in metadata and metadata[key] != actual:
            raise LibraryReadError("file_changed")
    return named


def read_audio_metadata(path: Path, *, music_root: Path | None = None) -> dict[str, object]:
    root = music_root.resolve(strict=True) if music_root else path.parent.resolve(strict=True)
    relative = path.relative_to(root)
    extension = extension_for(relative.as_posix())
    policy = FORMATS.get(extension)
    if policy is None:
        raise LibraryReadError("unsupported_extension")
    with open_library_file(root, relative.as_posix()) as file:
        before = os.fstat(file.fileno())
        values: dict[str, object] = {}
        parsed = False
        try:
            audio = MutagenFile(file, easy=False)
            if audio is not None and getattr(audio, "info", None) is not None:
                raw = getattr(audio, "tags", None)
                file.seek(0)
                easy = MutagenFile(file, easy=True)
                values = _read_tags(raw, getattr(easy, "tags", None))
                codec = {
                    "MP3": "mp3",
                    "FLAC": "flac",
                    "OggOpus": "opus",
                    "OggVorbis": "vorbis",
                    "OggFLAC": "flac",
                    "WavPack": "wavpack",
                }.get(type(audio).__name__)
                duration = _positive(audio.info.length)
                bitrate = _positive(getattr(audio.info, "bitrate", None))
                values.update(
                    codec=codec,
                    duration_seconds=duration,
                    bitrate=int(bitrate) if bitrate else None,
                    container=policy.demuxers[0],
                )
                parsed = codec in policy.codecs and duration is not None
        except (MutagenError, ValueError, TypeError, AttributeError, OverflowError):
            parsed = False
        fallback = not parsed
        if fallback or policy.inspect_streams:
            file.seek(0)
            technical = probe_metadata(_probe(file, path, policy), policy)
            for key, value in technical.items():
                if key in {"codec", "container", "duration_seconds", "bitrate"} or values.get(
                    key
                ) in (None, "", (), [], {}):
                    values[key] = value
        after = os.fstat(file.fileno())
        signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise LibraryReadError("file_changed")
        # An open descriptor remains valid when another process replaces its path.
        # Reopen through the same no-symlink traversal before accepting its metadata.
        try:
            with open_library_file(root, relative.as_posix()) as current:
                named = os.fstat(current.fileno())
        except (OSError, ValueError) as error:
            raise LibraryReadError("file_changed") from error
        if signature != (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns):
            raise LibraryReadError("file_changed")
    for key, value in filename_metadata(relative).items():
        if not values.get(key):
            values[key] = value
            if key in {"artist", "title", "album"} and value:
                fallback = True
    if not values.get("artist"):
        fallback = True
    values["artist"] = values.get("artist") or "Unknown Artist"
    values["version_signature"] = recording_version_signature(recording_title=str(values["title"]))
    values["file_extension"] = extension
    values["_metadata_fallback"] = fallback
    values["_file_size"] = after.st_size
    values["_file_mtime_ns"] = after.st_mtime_ns
    values["_file_device"] = after.st_dev
    values["_file_inode"] = after.st_ino
    return values
