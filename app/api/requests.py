from __future__ import annotations

import re
import shutil

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.api.dependencies import CsrfSession, CurrentSession
from app.db.models import Request as DbRequest
from app.db.models import RequestTrack
from app.schemas import ApprovalBody, CreateRequestBody, RefineRequestBody

router = APIRouter(prefix="/api/v1/requests", tags=["requests"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _idempotency(value: str | None) -> str:
    if value is None or not _IDEMPOTENCY.fullmatch(value):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Idempotency-Key must be 8-128 safe ASCII characters",
        )
    return value


def _request_payload(item: DbRequest, tracks: list[RequestTrack]) -> dict[str, object]:
    return {
        "id": item.id,
        "conversation_id": item.conversation_id,
        "action": item.action,
        "input_kind": item.input_kind,
        "text": item.raw_text,
        "status": item.status,
        "requested_count": item.requested_count,
        "discovered_count": item.discovered_count,
        "selected_count": item.selected_count,
        "error": (
            {"code": item.error_code, "message": item.error_message} if item.error_code else None
        ),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "tracks": [
            {
                "id": track.id,
                "ordinal": track.ordinal,
                "artist": track.artist,
                "title": track.title,
                "album": track.album,
                "album_artist": track.album_artist,
                "year": track.year,
                "duration_seconds": track.duration_seconds,
                "recording_mbid": track.recording_mbid,
                "release_mbid": track.release_mbid,
                "source_extractor": track.source_extractor,
                "source_id": track.source_id,
                "version": track.version_signature,
                "rationale": track.rationale,
                "duplicate_status": track.duplicate_status,
                "duplicate_track_id": track.duplicate_track_id,
                "selected": track.selected,
                "metadata_confidence": track.metadata_confidence,
            }
            for track in tracks
        ],
    }


@router.post("")
def create_request(
    body: CreateRequestBody,
    request: Request,
    authenticated: CsrfSession,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    try:
        result = request.app.state.requests.create(
            user_id=authenticated.user_id,
            text=body.text,
            action=body.action,
            conversation_id=body.conversation_id,
            idempotency_key=_idempotency(idempotency_key),
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    return {
        "request": _request_payload(result.request, []),
        "created": result.created,
        "url": f"/requests/{result.request.id}",
    }


@router.post("/{request_id}/refinements")
def refine_request(
    request_id: str,
    body: RefineRequestBody,
    request: Request,
    authenticated: CsrfSession,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, object]:
    parent = request.app.state.requests.get_for_user(request_id, authenticated.user_id)
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "request not found")
    try:
        result = request.app.state.requests.refine(
            user_id=authenticated.user_id,
            parent=parent,
            text=body.text,
            idempotency_key=_idempotency(idempotency_key),
        )
    except ValueError as error:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
    return {
        "request": _request_payload(result.request, []),
        "created": result.created,
        "url": f"/requests/{result.request.id}",
    }


@router.post("/{request_id}/approval")
def approve_request(
    request_id: str,
    body: ApprovalBody,
    request: Request,
    authenticated: CsrfSession,
) -> dict[str, object]:
    item = request.app.state.requests.get_for_user(request_id, authenticated.user_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "request not found")
    if (
        request.app.state.settings.initial_scan_required
        and not request.app.state.library.initial_scan_complete()
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "initial library scan is not complete")
    free_bytes = shutil.disk_usage(request.app.state.settings.music_path).free
    if free_bytes < request.app.state.settings.min_free_bytes:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, "insufficient free space")
    try:
        job_ids = request.app.state.jobs.queue_approved(
            request_id, authenticated.user_id, body.track_ids
        )
    except (ValueError, LookupError) as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
    return {"job_ids": job_ids, "count": len(job_ids)}


@router.get("/{request_id}")
def get_request(
    request_id: str, request: Request, authenticated: CurrentSession
) -> dict[str, object]:
    item = request.app.state.requests.get_for_user(request_id, authenticated.user_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "request not found")
    tracks = request.app.state.requests.tracks_for_request(request_id)
    return _request_payload(item, tracks)
