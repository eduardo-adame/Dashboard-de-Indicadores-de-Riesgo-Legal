"""Frontera HTTP de lectura de auditoría; no publica operaciones mutables."""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from app.audit.models import AuditPageResponse, AuditQuery, AuditUnavailableError
from app.audit.repository import AuditRepository
from app.audit.service import AuditQueryService
from app.config import get_settings
from app.security.api import current_principal, service_for
from app.security.models import AuthenticatedPrincipal, SecurityError


router = APIRouter(prefix="/api/audit", tags=["audit"])


def audit_service_for(request: Request) -> AuditQueryService:
    configured = getattr(request.app.state, "audit_service", None)
    if configured is not None:
        return configured
    settings = getattr(request.app.state, "audit_settings", None) or get_settings()
    return AuditQueryService(AuditRepository(settings.audit_psycopg_conninfo), service_for(request))


@router.get("/events", response_model=AuditPageResponse)
def list_events(
    occurred_from: datetime | None = None, occurred_to: datetime | None = None,
    action: str | None = Query(default=None, min_length=1, max_length=128),
    resource_type: str | None = Query(default=None, min_length=1, max_length=128),
    correlation_id: UUID | None = None, limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1024),
    principal: AuthenticatedPrincipal = Depends(current_principal),
    service: AuditQueryService = Depends(audit_service_for),
):
    try:
        query = AuditQuery(occurred_from=occurred_from, occurred_to=occurred_to, action=action,
                           resource_type=resource_type, correlation_id=correlation_id, limit=limit, cursor=cursor)
        return service.list_events(principal, query)
    except (ValidationError, ValueError):
        raise HTTPException(status_code=422, detail="Filtros de auditoría no válidos") from None
    except AuditUnavailableError:
        raise HTTPException(status_code=503, detail="Consulta de auditoría no disponible") from None
    except SecurityError:
        raise HTTPException(status_code=403, detail="Acceso no autorizado") from None
