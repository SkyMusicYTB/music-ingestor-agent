from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProposalTrack(StrictModel):
    artist: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    album: str | None
    album_artist: str | None
    year: int | None = Field(default=None, ge=1800, le=2200)
    duration_seconds: float | None = Field(default=None, gt=0, le=14_400)
    recording_mbid: str | None
    release_mbid: str | None
    release_group_mbid: str | None
    source_url: HttpUrl | None
    version: str | None
    rationale: str = Field(max_length=1000)
    evidence: list[str] = Field(max_length=10)
    confidence: float = Field(ge=0, le=1)


class MusicProposal(StrictModel):
    summary: str = Field(min_length=1, max_length=1000)
    clarification: str | None
    exhausted: bool
    tracks: list[ProposalTrack] = Field(max_length=250)


class CreateRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)
    action: Literal["find", "add"]
    conversation_id: str | None = None


class RefineRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    track_ids: list[str] = Field(min_length=1, max_length=100)
    acknowledge_rights: bool

    @model_validator(mode="after")
    def rights_acknowledged(self) -> ApprovalBody:
        if not self.acknowledge_rights:
            raise ValueError("rights acknowledgement is required")
        if len(set(self.track_ids)) != len(self.track_ids):
            raise ValueError("track IDs must be unique")
        return self
