from enum import StrEnum


class RequestAction(StrEnum):
    FIND = "find"
    ADD = "add"


class RequestStatus(StrEnum):
    PENDING = "pending"
    ORCHESTRATING = "orchestrating"
    PREVIEW = "preview"
    AUTO_QUEUED = "auto_queued"
    QUEUED = "queued"
    DEGRADED = "degraded"
    NEEDS_CLARIFICATION = "needs_clarification"
    REFUSED = "refused"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class TaskTarget(StrEnum):
    WEB = "web"
    WORKER = "worker"


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    RETRY_WAIT = "retry_wait"
    NEEDS_REVIEW = "needs_review"
    WAITING_FOR_SPACE = "waiting_for_space"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class JobStage(StrEnum):
    QUEUED = "queued"
    RESOLVING_SOURCE = "resolving_source"
    WAITING_AI = "waiting_ai"
    DOWNLOADING = "downloading"
    RESOLVING_METADATA = "resolving_metadata"
    FETCHING_ARTWORK = "fetching_artwork"
    TAGGING = "tagging"
    VERIFYING = "verifying"
    PUBLISHING = "publishing"
    COMPLETED = "completed"


class DuplicateStatus(StrEnum):
    NONE = "none"
    OWNED = "owned"
    POSSIBLE = "possible"


class ScanStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
