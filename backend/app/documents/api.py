"""Adaptadores administrativos para resultados OCR y reproceso documental."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import psycopg

from app.config import Settings
from app.coordination.kpi_integration import KpiIntegrationService
from app.coordination.kpi_repository import KpiIntegrationRepository
from app.coordination.runners import make_document_runner
from app.documents.api_models import OcrPage, OcrQuery, ReprocessRequest, ReprocessResponse
from app.documents.http_service import (
    DocumentHttpConflict, DocumentHttpService, DocumentHttpUnavailable, UpstreamCommittedKpiRetryable,
)
from app.documents.models import DocumentProcessingError
from app.documents.repository import DocumentsRepository
from app.security.api import current_principal, security_settings, service_for
from app.security.models import AuthenticatedPrincipal, AuditPersistenceError, SecurityError
from app.security.service import SecurityService


router = APIRouter(prefix="/api/documents", tags=["documents"])


def document_http_service_for(request: Request, security: SecurityService = Depends(service_for),
                              settings: Settings = Depends(security_settings)) -> DocumentHttpService:
    configured = getattr(request.app.state, "document_http_service", None)
    if configured is not None:
        return configured
    integration = getattr(request.app.state, "kpi_integration_service", None) or KpiIntegrationService(
        conninfo=settings.psycopg_conninfo, security=security,
        repository=KpiIntegrationRepository(settings.psycopg_conninfo))
    runner = getattr(request.app.state, "document_runner", None) or make_document_runner(
        conninfo=settings.psycopg_conninfo, security=security,
        storage_root=settings.ingestion_storage_root, kpi_integration=integration)
    return DocumentHttpService(DocumentsRepository(settings.psycopg_conninfo, security.repository),
                               security, runner=runner, kpi_integration=integration)


def _http_error(exc):
    if isinstance(exc, AuditPersistenceError):
        return HTTPException(503, "Operación no confirmada")
    if isinstance(exc, SecurityError):
        return HTTPException(403, "Acceso no autorizado")
    if isinstance(exc, ValueError):
        return HTTPException(422, "Parámetros no válidos")
    if isinstance(exc, DocumentHttpConflict):
        return HTTPException(409, "Fuente o intento no compatible")
    if isinstance(exc, UpstreamCommittedKpiRetryable):
        return HTTPException(503, {"cause": "UPSTREAM_COMMITTED_KPI_RETRYABLE",
                                   "operation_id": str(exc.operation_id)})
    return HTTPException(503, "Operación no confirmada")


@router.get("/ocr", response_model=OcrPage)
def list_ocr(query: OcrQuery = Query(),
             principal: AuthenticatedPrincipal = Depends(current_principal),
             service: DocumentHttpService = Depends(document_http_service_for)):
    try:
        return service.list_ocr(query, principal)
    except (SecurityError, ValueError, DocumentProcessingError, psycopg.Error) as exc:
        raise _http_error(exc) from None


@router.post("/{document_id}/ocr/reprocess", response_model=ReprocessResponse)
def reprocess_ocr(document_id: str, payload: ReprocessRequest,
                  principal: AuthenticatedPrincipal = Depends(current_principal),
                  service: DocumentHttpService = Depends(document_http_service_for)):
    try:
        return service.reprocess(document_id, payload, principal)
    except (SecurityError, ValueError, DocumentProcessingError, psycopg.Error) as exc:
        raise _http_error(exc) from None
