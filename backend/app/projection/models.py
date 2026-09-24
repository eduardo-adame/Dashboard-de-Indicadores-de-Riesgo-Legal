"""Contratos internos del módulo de Projection."""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.security.models import AuthenticatedPrincipal


class ProjectionError(Exception):
    """La proyección no puede confirmar un efecto analítico."""


class ProjectionInvariantError(ProjectionError):
    """Un retry no coincide con la aplicación analítica ya registrada."""


@dataclass(frozen=True)
class ProjectionContext:
    operation_id: UUID
    correlation_id: UUID
    actor: AuthenticatedPrincipal


@dataclass(frozen=True)
class ProjectionResult:
    entity_type: str
    business_id: str
    result: str
    payload_sha256: bytes
    source_record_id: UUID
    operation_id: UUID
    correlation_id: UUID
    recalculation_required: bool


@dataclass(frozen=True)
class SourceRecordProvenance:
    source_record_id: UUID
    ingest_file_id: UUID
