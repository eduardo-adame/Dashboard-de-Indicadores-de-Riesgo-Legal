"""Adaptadores HTTP para los casos de uso de ingesta."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.ingestion.models import IdempotencyConflictError, IngestionLimits, IngestionResult, RejectedFileError
from app.ingestion.repository import IngestionRepository
from app.ingestion.service import IngestionService
from app.security.api import require, security_settings, service_for
from app.security.models import AuthenticatedPrincipal, SecurityError
from app.security.service import SecurityService


router = APIRouter(prefix="/api/ingestion", tags=["ingestion"])


class IngestionResponse(BaseModel):
    file_id: str
    operation_id: str
    correlation_id: str
    state: str
    format: str | None
    family: str
    routing_target: str
    safe_cause_code: str | None
    idempotent: bool


class RunRequest(BaseModel):
    controlled_location: str = Field(min_length=1, max_length=128)


class RunResponse(BaseModel):
    processed: int
    results: list[IngestionResponse]


def _limits(settings: Settings) -> IngestionLimits:
    return IngestionLimits(
        settings.ingestion_max_file_bytes, settings.ingestion_max_archive_entries,
        settings.ingestion_max_archive_uncompressed_bytes, settings.ingestion_max_archive_entry_bytes,
        settings.ingestion_max_compression_ratio, settings.ingestion_csv_sample_bytes,
        settings.ingestion_stream_chunk_bytes, settings.ingestion_max_tabular_rows,
        settings.ingestion_max_tabular_columns, settings.ingestion_max_tabular_cells,
    )


def ingestion_service_for(
    request: Request,
    security: SecurityService = Depends(service_for),
    settings: Settings = Depends(security_settings),
) -> IngestionService:
    configured = getattr(request.app.state, "ingestion_service", None)
    if configured is not None:
        return configured
    return IngestionService(
        IngestionRepository(settings.psycopg_conninfo), security,
        Path(settings.ingestion_storage_root), _limits(settings),
    )


def _response(result: IngestionResult) -> IngestionResponse:
    return IngestionResponse(
        file_id=str(result.file_id), operation_id=str(result.operation_id),
        correlation_id=str(result.correlation_id), state=result.state, format=result.format,
        family=result.family.value, routing_target=result.routing_target.value,
        safe_cause_code=result.safe_cause_code, idempotent=result.idempotent,
    )


@router.post("/uploads", response_model=IngestionResponse, status_code=status.HTTP_201_CREATED)
def upload_file(
    file: Annotated[UploadFile, File()],
    controlled_location: Annotated[str, Form(min_length=1, max_length=128)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)],
    principal: AuthenticatedPrincipal = Depends(require("ingest.upload")),
    service: IngestionService = Depends(ingestion_service_for),
) -> IngestionResponse:
    try:
        result = service.ingest_stream(
            file.file, original_name=file.filename or "", controlled_location=controlled_location,
            principal=principal, capability="ingest.upload",
            source_locator=f"manual/{principal.account_id}/{idempotency_key}",
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError:
        raise HTTPException(status_code=409, detail="La clave de idempotencia corresponde a otro contenido") from None
    except RejectedFileError:
        raise HTTPException(status_code=422, detail="El archivo no pudo aceptarse para procesamiento") from None
    except SecurityError:
        raise HTTPException(status_code=403, detail="Acceso no autorizado") from None
    return _response(result)


@router.post("/runs", response_model=RunResponse)
def run_ingestion(
    payload: RunRequest,
    principal: AuthenticatedPrincipal = Depends(require("ingest.execute")),
    service: IngestionService = Depends(ingestion_service_for),
    settings: Settings = Depends(security_settings),
) -> RunResponse:
    try:
        results = service.run_location(Path(settings.ingestion_controlled_root), payload.controlled_location, principal)
    except RejectedFileError:
        raise HTTPException(status_code=422, detail="La ubicación controlada no es válida") from None
    except SecurityError:
        raise HTTPException(status_code=403, detail="Acceso no autorizado") from None
    return RunResponse(processed=len(results), results=[_response(result) for result in results])
