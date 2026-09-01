from __future__ import annotations

from app.workers.processor import DownloadJobProcessor


class DownloadPipeline(DownloadJobProcessor):
    """Named worker pipeline contract; implementation lives in the processor base."""
