"""Format-specific audio tag adapters."""

from app.tags.base import (
    TaggingError,
    UnsupportedMediaFormat,
    adapter_for_path,
    read_tags,
    write_tags,
)
from app.tags.models import EmbeddedArtwork, MediaTags, TrackTags

__all__ = [
    "EmbeddedArtwork",
    "MediaTags",
    "TaggingError",
    "TrackTags",
    "UnsupportedMediaFormat",
    "adapter_for_path",
    "read_tags",
    "write_tags",
]
