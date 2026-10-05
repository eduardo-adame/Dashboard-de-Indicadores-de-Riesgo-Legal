"""Contratos tipados de recuperación documental autorizada."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from decimal import Decimal
from uuid import UUID

from app.security.models import AuthenticatedPrincipal


class RetrievalError(RuntimeError):
    """Fallo seguro de recuperación, sin detalles del corpus."""


class EvidenceState(StrEnum):
    EVIDENCIA_SUFICIENTE = "EVIDENCIA_SUFICIENTE"
    EVIDENCIA_INSUFICIENTE = "EVIDENCIA_INSUFICIENTE"
    SIN_EVIDENCIA = "SIN_EVIDENCIA"


@dataclass(frozen=True)
class RetrievalContext:
    principal: AuthenticatedPrincipal
    query: str
    operation_id: UUID
    correlation_id: UUID
    context_reference_id: UUID | None = None


@dataclass(frozen=True)
class CitationMetadata:
    document_id: str
    document_name: str
    document_type: str
    document_date: date | None
    fragment_id: UUID
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    clause: str | None = None

    @property
    def complete(self) -> bool:
        return bool(
            self.document_id and self.document_name and self.document_type
            and self.document_date is not None and self.fragment_id
        )


@dataclass(frozen=True)
class RetrievedCandidate:
    fragment_id: UUID
    fragment_text: str
    citation: CitationMetadata
    rank: int
    score: float


@dataclass(frozen=True)
class RankedFragment:
    fragment_id: UUID
    document_id: str
    fragment_text: str
    citation: CitationMetadata
    vector_rank: int | None
    bm25_rank: int | None
    rrf_score: Decimal


@dataclass(frozen=True)
class StructuredContext:
    fragments: tuple[RankedFragment, ...]


@dataclass(frozen=True)
class RetrievalResult:
    operation_id: UUID
    correlation_id: UUID
    state: EvidenceState
    fragments: tuple[RankedFragment, ...]
    structured_context: StructuredContext
    context_reference_id: UUID | None
