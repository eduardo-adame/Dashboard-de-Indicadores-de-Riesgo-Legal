"""Frontera HTTP de consultas RAG autorizadas."""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.config import get_settings
from app.rag.provider import GroqProvider
from app.rag.retrieval import RetrievalService
from app.rag.service import RagApplicationError, RagApplicationService
from app.security.api import configured_security_service, current_principal, service_for
from app.security.models import AuthenticatedPrincipal


router = APIRouter(prefix="/api/rag", tags=["rag"])


class RagQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4096)
    context_reference_id: UUID | None = None


class RagFragmentResponse(BaseModel):
    fragment_id: UUID
    fragment_text: str
    usage: str
    document_id: str
    document_name: str
    document_type: str
    document_date: date | None
    page_start: int | None
    page_end: int | None
    section: str | None
    clause: str | None


class RagCitationResponse(BaseModel):
    handle: str
    fragment_id: UUID
    document_id: str
    document_name: str
    document_type: str
    document_date: date | None
    page_start: int | None
    page_end: int | None
    section: str | None
    clause: str | None


class RagQueryResponse(BaseModel):
    operation_id: UUID
    correlation_id: UUID
    state: str | None
    operation_status: str
    generation_status: str
    generated_response: str | None
    safe_result_message: str | None
    context_reference_id: UUID | None
    fragments: list[RagFragmentResponse]
    citations: list[RagCitationResponse]


@lru_cache(maxsize=1)
def _configured_service() -> RagApplicationService:
    settings = get_settings()
    security = configured_security_service()
    return RagApplicationService(
        conninfo=settings.psycopg_conninfo,
        security=security,
        retrieval=RetrievalService(security),
        provider=GroqProvider(
            api_key=settings.groq_api_key.get_secret_value() if settings.groq_api_key else None,
            timeout_seconds=settings.groq_timeout_seconds,
            max_request_bytes=settings.groq_max_request_bytes,
        ),
    )


def application_service_for(request: Request) -> RagApplicationService:
    configured = getattr(request.app.state, "rag_application_service", None)
    if configured is not None:
        return configured
    # La instancia de Security inyectada en pruebas debe ser la misma del dominio.
    security = getattr(request.app.state, "security_service", None)
    if security is None:
        return _configured_service()
    settings = get_settings()
    return RagApplicationService(
        conninfo=settings.psycopg_conninfo,
        security=service_for(request),
        retrieval=RetrievalService(security),
        provider=GroqProvider(
            api_key=settings.groq_api_key.get_secret_value() if settings.groq_api_key else None,
            timeout_seconds=settings.groq_timeout_seconds,
            max_request_bytes=settings.groq_max_request_bytes,
        ),
    )


@router.post("/query", response_model=RagQueryResponse)
def query_rag(
    payload: RagQueryRequest,
    response: Response,
    principal: AuthenticatedPrincipal = Depends(current_principal),
    idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
    service: RagApplicationService = Depends(application_service_for),
) -> RagQueryResponse:
    try:
        result = service.execute(
            principal=principal,
            query=payload.query,
            idempotency_key=idempotency_key,
            context_reference_id=payload.context_reference_id,
        )
    except RagApplicationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    response.status_code = result.status_code
    return RagQueryResponse.model_validate(result.payload)
