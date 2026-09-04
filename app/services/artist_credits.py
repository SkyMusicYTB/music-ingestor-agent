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
_ARTIST_PUNCTUATION = (("&", " joinampersand "), (",", " joincomma "), (";", " joinsemicolon "))
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
    """Normalize a whole artist credit without inventing collaborator boundaries.

    Commas and ampersands are meaningful parts of singleton names such as
    ``Earth, Wind & Fire`` and ``Tyler, the Creator``.  Preserve those distinctions
    here; collaboration separator equivalence is enabled separately only when a
    provider or MusicBrainz supplied a structured multi-artist credit.
    """

    for punctuation, marker in _ARTIST_PUNCTUATION:
        value = value.replace(punctuation, marker)
    decomposed = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", value.casefold())).strip()


def artist_credit_variant(value: str, *, artists: Sequence[str] = ()) -> str:
    """Return one collaboration query variant backed by structured artist data."""

    bounded = structured_artists(artists)
    if len(bounded) > 1:
        return " & ".join(bounded)
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def _collaboration_credit_key(value: str) -> str:
    """Normalize separators only after structured metadata proves collaboration."""

    decomposed = unicodedata.normalize("NFKD", _JOIN_RE.sub(" ", value))
    value = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", _PUNCT_RE.sub(" ", value.casefold())).strip()


def artist_credit_similarity(
    left: str,
    right: str,
    *,
    left_artists: Sequence[str] = (),
    right_artists: Sequence[str] = (),
) -> float:
    left_items = structured_artists(left_artists)
    right_items = structured_artists(right_artists)
    left_key, right_key = artist_credit_key(left), artist_credit_key(right)
    if not left_key or not right_key:
        return 0.0
    # Unlike token_set_ratio, the full-credit ratio never grants a perfect score
    # merely because one credit contains another (and an additional performer).
    score = ratio(left_key, right_key) / 100.0
    if len(left_items) > 1 or len(right_items) > 1:
        score = max(
            score,
            ratio(_collaboration_credit_key(left), _collaboration_credit_key(right)) / 100.0,
        )
    if left_items and right_items and len(left_items) != len(right_items):
        return min(score, 0.94)
    structured_equivalent = False
    if left_items and right_items:
        remaining = [artist_credit_key(item) for item in right_items]
        matches: list[float] = []
        for item in left_items:
            key = artist_credit_key(item)
            best = max(range(len(remaining)), key=lambda i: ratio(key, remaining[i]))
            matches.append(ratio(key, remaining.pop(best)) / 100.0)
        # Reordered structured credits are equivalent only when every artist
        # agrees. A missing collaborator cannot be hidden by an average score.
        structured_score = min(matches)
        score = max(score, structured_score)
        structured_equivalent = structured_score >= 0.95
    left_tokens = set(left_key.split())
    right_tokens = set(right_key.split())
    if (
        left_key != right_key
        and left_tokens != right_tokens
        and (left_tokens < right_tokens or right_tokens < left_tokens)
        and not structured_equivalent
    ):
        # A long shared credit must not make an added or missing short collaborator
        # disappear behind a high fuzzy ratio. Exact separator variants already
        # normalize to the same key and do not need this exception.
        score = min(score, 0.94)
    return score
