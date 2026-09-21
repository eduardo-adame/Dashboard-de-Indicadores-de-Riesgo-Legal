"""Identidad, versión documental y procesamiento OCR."""
from app.documents.models import (
    DocumentNotFoundError,
    DocumentProcessingError,
    DocumentRef,
    DocumentVersionRef,
    DocumentVersionState,
    StaleWriteError,
    VersionNotReadyError,
)
from app.documents.management import OcrStatus, can_reprocess, reprocess_preserves_history
from app.documents.repository import DocumentsRepository
from app.documents.service import DocumentOperationContext, DocumentsService
from app.documents.versioning import ensure_candidate_ready, expected_active_matches

__all__ = (
    "DocumentNotFoundError",
    "DocumentOperationContext",
    "DocumentProcessingError",
    "DocumentRef",
    "DocumentVersionRef",
    "DocumentVersionState",
    "DocumentsRepository",
    "DocumentsService",
    "OcrStatus",
    "StaleWriteError",
    "VersionNotReadyError",
    "can_reprocess",
    "ensure_candidate_ready",
    "expected_active_matches",
    "reprocess_preserves_history",
)
