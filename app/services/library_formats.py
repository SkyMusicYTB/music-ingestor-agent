"""Read-only library support, deliberately independent of writable acquisition formats.

Each advertised container/codec pair has an offline synthetic fixture. APE and
Musepack remain recognizable audit candidates, but are not claimed as supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

PARSER_VERSION = 2


@dataclass(frozen=True, slots=True)
class LibraryFormat:
    label: str
    demuxers: tuple[str, ...]
    codecs: frozenset[str]
    inspect_streams: bool = False


_PCM = frozenset({"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le"})
_AIFF_PCM = frozenset({"pcm_s16be", "pcm_s24be", "pcm_s32be", "pcm_s16le"})
_MP4 = LibraryFormat("MP4", ("mov",), frozenset({"aac", "alac"}), True)
_OGG = LibraryFormat("OGG", ("ogg",), frozenset({"vorbis", "opus", "flac"}), True)
_AIFF = LibraryFormat("AIFF", ("aiff",), _AIFF_PCM, True)
_ASF = LibraryFormat("WMA", ("asf",), frozenset({"wmav2"}), True)
FORMATS: dict[str, LibraryFormat] = {
    ".mp3": LibraryFormat("MP3", ("mp3",), frozenset({"mp3"})),
    ".m4a": _MP4,
    ".mp4": _MP4,
    ".m4b": _MP4,
    ".flac": LibraryFormat("FLAC", ("flac",), frozenset({"flac"})),
    ".ogg": _OGG,
    ".oga": _OGG,
    ".opus": LibraryFormat("OGG", ("ogg",), frozenset({"opus"}), True),
    ".wav": LibraryFormat("WAV", ("wav",), _PCM, True),
    ".aac": LibraryFormat("AAC", ("aac",), frozenset({"aac"}), True),
    ".aif": _AIFF,
    ".aiff": _AIFF,
    ".aifc": _AIFF,
    ".wma": _ASF,
    ".asf": _ASF,
    ".wv": LibraryFormat("WAVPACK", ("wv",), frozenset({"wavpack"})),
    ".webm": LibraryFormat("WEBM", ("matroska", "webm"), frozenset({"opus", "vorbis"}), True),
    ".mka": LibraryFormat(
        "MATROSKA",
        ("matroska", "webm"),
        frozenset({"aac", "alac", "flac", "mp3", "opus", "vorbis"}) | _PCM,
        True,
    ),
}
SUPPORTED_EXTENSIONS = frozenset(FORMATS)
SUPPORTED_CODECS = frozenset(codec for policy in FORMATS.values() for codec in policy.codecs)
DEFERRED_EXTENSIONS = frozenset({".ape", ".mpc"})
IGNORED_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".svg",
        ".bmp",
        ".m3u",
        ".m3u8",
        ".pls",
        ".cue",
        ".txt",
        ".nfo",
        ".db",
        ".sqlite",
        ".part",
        ".tmp",
        ".json",
    }
)


def extension_for(path: str) -> str:
    return PurePosixPath(path).suffix.casefold()[:16]


def format_label(extension: str | None) -> str:
    return (extension or "").removeprefix(".").upper() or "AUDIO"
