"""Adaptador HTTP de la coordinación de despacho downstream.

El llamante sólo aporta ``file_id`` y ``correlation_id``; el destino
(``VALIDATION`` o ``DOCUMENT``) se reconstruye de forma autoritativa desde el
estado persistido.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.coordination.repository import CoordinationRepository
from app.coordination.service import CoordinationService
from app.security.api import require, security_settings, service_for
from app.security.models import AuthenticatedPrincipal, SecurityError
from app.security.service import SecurityService

router = APIRouter(prefix="/api/coordination", tags=["coordination"])


class DispatchRequest(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)
    correlation_id: str | None = Field(default=None, max_length=64)


class DispatchResponse(BaseModel):
    downstream_target: str | None
    operation_id: str | None
    downstream_result_id: str | None
    state: str


def coordination_service_for(
    request: Request,
    security: SecurityService = Depends(service_for),
    settings: Settings = Depends(security_settings),
) -> CoordinationService:
    configured = getattr(request.app.state, "coordination_service", None)
    if configured is not None:
        return configured
    from app.coordination.runners import make_document_runner, make_validation_runner

    repository = CoordinationRepository(settings.psycopg_conninfo, security.repository)
    validation_runner = getattr(request.app.state, "validation_runner", None) or make_validation_runner(
        conninfo=settings.psycopg_conninfo, security=security
    )
    document_runner = getattr(request.app.state, "document_runner", None) or make_document_runner(
        conninfo=settings.psycopg_conninfo, security=security, storage_root=settings.ingestion_storage_root
    )
    return CoordinationService(
        repository,
        security,
        validation_runner=validation_runner,
        document_runner=document_runner,
        stale_threshold_seconds=settings.coordination_stale_threshold_seconds,
    )


@router.post("/dispatch", response_model=DispatchResponse)
def dispatch_result(
    payload: DispatchRequest,
    principal: AuthenticatedPrincipal = Depends(require("ingest.execute")),
    service: CoordinationService = Depends(coordination_service_for),
) -> DispatchResponse:
    try:
        file_id = UUID(payload.file_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Identificador de archivo no válido") from None
    try:
        correlation_id = UUID(payload.correlation_id) if payload.correlation_id else uuid4()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Identificador de correlación no válido") from None

    try:
        outcome = service.dispatch(file_id=file_id, correlation_id=correlation_id, actor=principal)
    except SecurityError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso no autorizado") from None
    return DispatchResponse(
        downstream_target=outcome.downstream_target,
        operation_id=str(outcome.operation_id) if outcome.operation_id else None,
        downstream_result_id=str(outcome.downstream_result_id) if outcome.downstream_result_id else None,
        state=outcome.state,
    )
