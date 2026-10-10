"""Frontera pública canónica para reinyección de cuarentena."""
from __future__ import annotations

import base64
from datetime import datetime
import json
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
import psycopg

from app.config import Settings
from app.coordination.kpi_integration import KpiIntegrationError, KpiIntegrationService
from app.coordination.kpi_repository import KpiIntegrationRepository
from app.security.api import current_principal, require, security_settings, service_for
from app.security.models import AuditPersistenceError, AuthenticatedPrincipal, SecurityError
from app.security.service import SecurityService
from app.validation.quarantine import QuarantineError
from app.validation.repository import ValidationRepository
from app.validation.service import ValidationContext, ValidationService
from app.validation.models import QuarantineCause, QuarantineState
from app.ingestion.models import SourceFamily


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


_CAUSE_DESCRIPTIONS = {
    QuarantineCause.MISSING_REQUIRED_FIELD: "Falta un campo obligatorio",
    QuarantineCause.INVALID_TYPE: "Tipo de dato no válido",
    QuarantineCause.INVALID_DATE: "Fecha no válida",
    QuarantineCause.OUT_OF_CATALOG: "Valor fuera del catálogo",
    QuarantineCause.NEGATIVE_AMOUNT: "Importe negativo",
    QuarantineCause.DATE_ORDER_VIOLATION: "Orden de fechas no válido",
    QuarantineCause.STRUCTURAL_INCONSISTENCY: "Estructura inconsistente",
    QuarantineCause.IDENTITY_CONFLICT: "Conflicto de identidad",
    QuarantineCause.TECHNICAL_READ_FAILURE: "Fallo técnico de lectura",
    QuarantineCause.OTHER_CAUSE: "Otra causa de rechazo",
    QuarantineCause.FORMAT_MISMATCH: "El formato detectado no coincide con la extensión declarada",
    QuarantineCause.ARCHIVE_LIMIT_EXCEEDED: "El contenedor supera los límites de tamaño o entradas",
    QuarantineCause.ARCHIVE_COMPRESSION_RATIO_EXCEEDED: "El contenedor supera el límite de compresión",
    QuarantineCause.CORRUPT_ARCHIVE: "El contenedor está corrupto o no puede leerse",
    QuarantineCause.BINARY_CONTENT: "El archivo tabular contiene datos binarios",
    QuarantineCause.UNSUPPORTED_ENCODING: "La codificación del archivo no está admitida",
    QuarantineCause.UNDETERMINABLE_STRUCTURE: "No puede determinarse una estructura tabular consistente",
    QuarantineCause.AMBIGUOUS_DELIMITER: "El separador de campos es ambiguo",
    QuarantineCause.PROTECTED_PDF: "El PDF está protegido y no puede leerse",
    QuarantineCause.CORRUPT_PDF: "La estructura del PDF está corrupta",
    QuarantineCause.UNSUPPORTED_FORMAT: "El contenido no corresponde a un formato admitido",
    QuarantineCause.TABULAR_LIMIT_EXCEEDED: "El contenido tabular supera los límites de filas, columnas o celdas",
    QuarantineCause.PROTECTED_DOCUMENT: "El documento está protegido y no puede extraerse",
    QuarantineCause.EMPTY_DOCUMENT: "El documento no contiene contenido extraíble",
    QuarantineCause.TECHNICAL_FAILURE: "Fallo técnico de procesamiento",
}


class QuarantineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    ingest_file_id: UUID | None
    source_record_id: UUID | None
    row_number: int | None
    source_family: SourceFamily | None
    created_at: AwareDatetime
    cause_code: QuarantineCause
    cause_description: str
    state: QuarantineState
    original_payload: dict[str, object] | None
    candidate_payload: dict[str, object] | None
    discard_justification: str | None


class QuarantinePageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[QuarantineResponse]
    next_cursor: str | None


class DiscardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    justification: str = Field(min_length=1)

    @field_validator("justification")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("La justificación es obligatoria")
        return value


class DiscardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    state: QuarantineState
    discard_justification: str


def validation_service_for(
    request: Request, security: SecurityService = Depends(service_for),
    settings: Settings = Depends(security_settings),
) -> ValidationService:
    return getattr(request.app.state, "validation_service", None) or ValidationService(
        ValidationRepository(settings.psycopg_conninfo, security.repository), security,
    )


def _decode_cursor(value: str | None) -> tuple[datetime, UUID] | None:
    if value is None:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        stamp, identity = json.loads(raw)
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError
        return moment, UUID(identity)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="Cursor no válido") from None


def _encode_cursor(row: dict[str, object]) -> str:
    raw = json.dumps([row["created_at"].isoformat(), str(row["id"])]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@router.get("/quarantine", response_model=QuarantinePageResponse)
def list_quarantine(
    ingest_file_id: UUID | None = None, source_family: SourceFamily | None = None,
    rejected_from: AwareDatetime | None = None, rejected_to: AwareDatetime | None = None,
    cause_code: QuarantineCause | None = None, state: QuarantineState | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: AuthenticatedPrincipal = Depends(current_principal),
    service: ValidationService = Depends(validation_service_for),
) -> QuarantinePageResponse:
    after = _decode_cursor(cursor)
    if rejected_from is not None and rejected_to is not None and rejected_from > rejected_to:
        raise HTTPException(status_code=422, detail="Rango de fechas no válido")
    context = ValidationContext(uuid4(), uuid4(), principal)
    try:
        rows, has_more = service.quarantine_page(
            context=context, ingest_file_id=ingest_file_id, source_family=source_family,
            rejected_from=rejected_from, rejected_to=rejected_to, cause=cause_code,
            state=state, limit=limit, after=after,
        )
    except AuditPersistenceError:
        raise HTTPException(status_code=503, detail="La consulta no puede confirmarse") from None
    except SecurityError:
        raise HTTPException(status_code=403, detail="Acceso no autorizado") from None
    except (psycopg.Error, RuntimeError):
        raise HTTPException(status_code=503, detail="La consulta no puede confirmarse") from None
    items = [QuarantineResponse(
        **{key: row[key] for key in QuarantineResponse.model_fields if key != "cause_description"},
        cause_description=_CAUSE_DESCRIPTIONS[QuarantineCause(row["cause_code"])],
    ) for row in rows]
    return QuarantinePageResponse(items=items, next_cursor=_encode_cursor(rows[-1]) if has_more else None)


@router.post("/quarantine/{item_id}/discard", response_model=DiscardResponse)
def discard(
    item_id: UUID, payload: DiscardRequest,
    principal: AuthenticatedPrincipal = Depends(current_principal),
    service: ValidationService = Depends(validation_service_for),
) -> DiscardResponse:
    try:
        item = service.discard_for_http(
            item_id=item_id, justification=payload.justification,
            context=ValidationContext(uuid4(), uuid4(), principal),
        )
    except AuditPersistenceError:
        raise HTTPException(status_code=503, detail="El descarte no puede confirmarse") from None
    except SecurityError:
        raise HTTPException(status_code=403, detail="Acceso no autorizado") from None
    except QuarantineError:
        raise HTTPException(status_code=409, detail="El descarte no puede confirmarse") from None
    except (psycopg.Error, RuntimeError):
        raise HTTPException(status_code=503, detail="El descarte no puede confirmarse") from None
    return DiscardResponse(id=item.id, state=item.state, discard_justification=item.discard_justification)
