"""Frontera pública canónica para reinyección de cuarentena."""
from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import Settings
from app.coordination.kpi_integration import KpiIntegrationError, KpiIntegrationService
from app.coordination.kpi_repository import KpiIntegrationRepository
from app.security.api import require, security_settings, service_for
from app.security.models import AuthenticatedPrincipal, SecurityError
from app.security.service import SecurityService
from app.validation.quarantine import QuarantineError
from app.validation.repository import ValidationRepository
from app.validation.service import ValidationService


router = APIRouter(prefix="/api/validation", tags=["validation"])


class ReinjectionRequest(BaseModel):
    corrected_payload: dict[str, object] = Field(min_length=1)
    correlation_id: str | None = Field(default=None, max_length=64)


class ReinjectionResponse(BaseModel):
    quarantine_state: str | None
    job_id: str
    operation_id: str
    job_state: str


def reinjection_service_for(
    request: Request,
    security: SecurityService = Depends(service_for),
    settings: Settings = Depends(security_settings),
) -> tuple[KpiIntegrationService, ValidationService]:
    integration = getattr(request.app.state, "kpi_integration_service", None) or KpiIntegrationService(
        conninfo=settings.psycopg_conninfo,
        security=security,
        repository=KpiIntegrationRepository(settings.psycopg_conninfo),
    )
    validation = ValidationService(
        ValidationRepository(settings.psycopg_conninfo, security.repository), security
    )
    return integration, validation


@router.patch("/quarantine/{item_id}/reinject", response_model=ReinjectionResponse)
def reinject(
    item_id: UUID,
    payload: ReinjectionRequest,
    principal: AuthenticatedPrincipal = Depends(require("quarantine.reinject")),
    services: tuple[KpiIntegrationService, ValidationService] = Depends(reinjection_service_for),
) -> ReinjectionResponse:
    try:
        correlation_id = UUID(payload.correlation_id) if payload.correlation_id else uuid4()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Identificador de correlación no válido") from None
    integration, validation = services
    try:
        outcome = integration.reinject(
            item_id=item_id,
            corrected_payload=payload.corrected_payload,
            correlation_id=correlation_id,
            actor=principal,
            validation=validation,
        )
    except SecurityError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso no autorizado") from None
    except QuarantineError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La reinyección no puede confirmarse") from None
    except KpiIntegrationError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="La reinyección fue confirmada; el recálculo sigue pendiente") from None
    return ReinjectionResponse(
        quarantine_state=outcome.quarantine_state,
        job_id=str(outcome.job_id),
        operation_id=str(outcome.operation_id),
        job_state=outcome.state,
    )
