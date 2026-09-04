from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.sources.identities import ExtractorIdentity, ProviderIdentity, UploaderRelationship
from app.sources.providers import MAX_PROVIDER_DESCRIPTION_LENGTH

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SAFE_EXTRACTOR = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,99}$")
_URL_CONTROL = re.compile(r"[\x00-\x20\x7f]")
FiniteScore = Annotated[
    float,
    Field(ge=0.0, le=1.0, allow_inf_nan=False, strict=True),
]
PositiveDuration = Annotated[
    float,
    Field(gt=0.0, le=14_400.0, allow_inf_nan=False, strict=True),
]


class SourceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class EvidenceReference(SourceModel):
    reference_id: str = Field(min_length=1, max_length=200, pattern=_SAFE_ID.pattern)
    provider: ProviderIdentity
    external_id: str = Field(min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2_048)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PROVIDER_DESCRIPTION_LENGTH,
    )

    @field_validator("url")
    @classmethod
    def url_has_no_controls(cls, value: str | None) -> str | None:
        if value is not None and (_URL_CONTROL.search(value) or "\\" in value):
            raise ValueError("evidence URL contains forbidden characters")
        return value

    @property
    def evidence_id(self) -> str:
        return self.reference_id


class SourceCandidate(SourceModel):
    source_id: str = Field(min_length=1, max_length=200, pattern=_SAFE_ID.pattern)
    provider: ProviderIdentity
    extractor: str = Field(min_length=1, max_length=100, pattern=_SAFE_EXTRACTOR.pattern)
    url: str = Field(min_length=1, max_length=2_048)
    title: str = Field(min_length=1, max_length=500)
    artist: str | None = Field(default=None, min_length=1, max_length=300)
    artists: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        default=(), max_length=12
    )
    artist_source: Literal["artist", "album_artist", "creator", "parsed_title"] | None = None
    track: str | None = Field(default=None, min_length=1, max_length=300)
    version: str | None = Field(default=None, min_length=1, max_length=100)
    duration_seconds: PositiveDuration | None = None
    uploader_name: str | None = Field(default=None, min_length=1, max_length=300)
    uploader_id: str | None = Field(default=None, min_length=1, max_length=200)
    uploader_relationship: UploaderRelationship = UploaderRelationship.UNKNOWN
    audio_available: bool = True
    audio_quality: FiniteScore = 1.0
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_PROVIDER_DESCRIPTION_LENGTH,
    )
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=20)

    @field_validator("extractor")
    @classmethod
    def normalize_extractor(cls, value: str) -> str:
        return value.casefold()

    @field_validator("url")
    @classmethod
    def url_has_no_controls(cls, value: str) -> str:
        if _URL_CONTROL.search(value) or "\\" in value:
            raise ValueError("source URL contains forbidden characters")
        return value

    @property
    def identity(self) -> ExtractorIdentity:
        return ExtractorIdentity(
            provider=self.provider,
            extractor=self.extractor,
            source_id=self.source_id,
        )

    @property
    def channel(self) -> str | None:
        return self.uploader_name


class SourceIntent(SourceModel):
    artist: str = Field(min_length=1, max_length=300)
    artists: tuple[Annotated[str, Field(min_length=1, max_length=300)], ...] = Field(
        default=(), max_length=12
    )
    title: str = Field(min_length=1, max_length=300)
    requested_version: str = Field(
        default="studio",
        validation_alias=AliasChoices("requested_version", "version", "version_signature"),
        min_length=1,
        max_length=100,
    )
    duration_seconds: PositiveDuration | None = None

    @property
    def version_signature(self) -> str:
        return self.requested_version


class SourcePolicy(SourceModel):
    max_candidates: int = Field(
        default=24,
        validation_alias=AliasChoices("max_candidates", "max_source_candidates"),
        ge=1,
        le=100,
        strict=True,
    )
    visible_candidates: int = Field(
        default=5,
        validation_alias=AliasChoices("visible_candidates", "visible_source_candidates"),
        ge=1,
        le=25,
        strict=True,
    )
    max_attempts: int = Field(
        default=3,
        validation_alias=AliasChoices("max_attempts", "max_source_attempts"),
        ge=1,
        le=10,
        strict=True,
    )
    auto_threshold: FiniteScore = Field(
        default=0.88,
        validation_alias=AliasChoices("auto_threshold", "automatic_threshold"),
    )
    minimum_lead: FiniteScore = Field(
        default=0.08,
        validation_alias=AliasChoices("minimum_lead", "ambiguity_margin"),
    )
    ai_confidence_threshold: FiniteScore = 0.90
    ai_local_score_threshold: FiniteScore = 0.75
    missing_duration_ai_confidence: FiniteScore = 0.94
    duration_tolerance_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    duration_tolerance_ratio: float = Field(default=0.05, gt=0, le=1, allow_inf_nan=False)
    allowed_providers: tuple[ProviderIdentity, ...] = (
        ProviderIdentity.YOUTUBE,
        ProviderIdentity.SOUNDCLOUD,
        ProviderIdentity.BANDCAMP,
    )
    provider_preference: tuple[ProviderIdentity, ...] = (
        ProviderIdentity.BANDCAMP,
        ProviderIdentity.SOUNDCLOUD,
        ProviderIdentity.YOUTUBE,
    )

    @model_validator(mode="after")
    def coherent_limits(self) -> SourcePolicy:
        if self.visible_candidates > self.max_candidates:
            raise ValueError("visible_candidates cannot exceed max_candidates")
        if self.max_attempts > self.max_candidates:
            raise ValueError("max_attempts cannot exceed max_candidates")
        if len(set(self.allowed_providers)) != len(self.allowed_providers):
            raise ValueError("allowed_providers must be unique")
        if len(set(self.provider_preference)) != len(self.provider_preference):
            raise ValueError("provider_preference must be unique")
        return self

    @property
    def max_source_candidates(self) -> int:
        return self.max_candidates

    @property
    def visible_source_candidates(self) -> int:
        return self.visible_candidates

    @property
    def max_source_attempts(self) -> int:
        return self.max_attempts

    @property
    def automatic_threshold(self) -> float:
        return self.auto_threshold

    @property
    def ambiguity_margin(self) -> float:
        return self.minimum_lead


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


TrackIntent = SourceIntent
