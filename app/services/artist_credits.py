from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from rapidfuzz.fuzz import ratio

# Separators are comparison/search hints, not a parser for individual artists.
# In particular, "Earth, Wind & Fire" must remain one structured artist when
# that is the name supplied by the provider or by MusicBrainz's artist credit.
_JOIN_RE = re.compile(r"\s*(?:[,;&]|\bfeat\.?\s|\bfeaturing\s)\s*|\s+(?:and|x|with)\s+", re.I)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
MAX_STRUCTURED_ARTISTS = 8


def structured_artists(value: object) -> tuple[str, ...]:
    """Keep bounded provider artist arrays without splitting legitimate names."""
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value[:MAX_STRUCTURED_ARTISTS]:
        if not isinstance(item, str):
            continue
        name = unicodedata.normalize("NFKC", item).strip()
        if not name or len(name) > 300 or any(unicodedata.category(c)[0] == "C" for c in name):
            continue
        key = name.casefold()
        if key not in seen:
            result.append(name)
            seen.add(key)
    return tuple(result)


def artist_credit_key(value: str) -> str:
    """Compare entire credits while retaining every performer-bearing token."""
    value = _JOIN_RE.sub(" ", value)
    decomposed = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", value.casefold())).strip()


def artist_credit_variant(value: str) -> str:
    """One alternate Lucene phrase; never an authoritative artist decomposition."""
    return _SPACE_RE.sub(" ", _JOIN_RE.sub(" & ", value)).strip()


def artist_credit_similarity(
    left: str,
    right: str,
    *,
    left_artists: Sequence[str] = (),
    right_artists: Sequence[str] = (),
) -> float:
    left_key, right_key = artist_credit_key(left), artist_credit_key(right)
    if not left_key or not right_key:
        return 0.0
    # Unlike token_set_ratio, the full-credit ratio never grants a perfect score
    # merely because one credit contains another (and an additional performer).
    score = ratio(left_key, right_key) / 100.0
    if left_artists and right_artists and len(left_artists) == len(right_artists):
        remaining = [artist_credit_key(item) for item in right_artists]
        matches: list[float] = []
        for item in left_artists:
            key = artist_credit_key(item)
            best = max(range(len(remaining)), key=lambda i: ratio(key, remaining[i]))
            matches.append(ratio(key, remaining.pop(best)) / 100.0)
        # Reordered structured credits are equivalent only when every artist
        # agrees. A missing collaborator cannot be hidden by an average score.
        score = max(score, min(matches))
    return score
