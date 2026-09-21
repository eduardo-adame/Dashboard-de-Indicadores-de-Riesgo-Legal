"""Tipos del dominio de coordinación de despacho."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class CoordinationError(Exception):
    """Error controlado de la coordinación."""


class DispatchConflictError(CoordinationError):
    """El despacho ya está en curso."""


class DispatchState(StrEnum):
    """Estados del despacho (coinciden con el esquema físico)."""

    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RoutingTarget(StrEnum):
    """Destino downstream reconstruido autoritativamente."""

    VALIDATION = "VALIDATION"
    DOCUMENT = "DOCUMENT"


@dataclass(frozen=True)
class CoordinationDispatch:
    id: UUID
    operation_id: UUID
    file_id: UUID
    downstream_target: str
    state: DispatchState
    attempt_count: int
    correlation_id: UUID
    downstream_result_id: UUID | None
    safe_cause_code: str | None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None
