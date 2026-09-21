"""Cuarentena lógica unificada: estados, transiciones y reglas de integridad.

Preserva separadamente el contenido original y el candidato corregido, y no
resuelve conflictos por sobrescritura. Las reglas de transición y de
justificación de descarte se aplican aquí.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from app.validation.models import QuarantineCause, QuarantineState


class QuarantineError(Exception):
    """Error de transición inválida en la cuarentena."""


@dataclass(frozen=True)
class QuarantineItem:
    """Elemento retenido en la cuarentena unificada."""

    id: UUID
    ingest_file_id: UUID | None
    source_record_id: UUID | None
    cause: QuarantineCause
    state: QuarantineState
    operation_id: UUID
    correlation_id: UUID
    original_payload: dict[str, object] | None = None
    candidate_payload: dict[str, object] | None = None
    discard_justification: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class QuarantineTransition:
    """Transición de estado registrada para trazabilidad."""

    item_id: UUID
    from_state: QuarantineState
    to_state: QuarantineState
    operation_id: UUID
    correlation_id: UUID
    actor_identifier: str
    actor_type: str = "HUMAN"
    occurred_at: datetime | None = None


def reinject(item: QuarantineItem, corrected_payload: dict[str, object]) -> QuarantineItem:
    """Solicita reinyección de un elemento ``Pendiente``.

    Conserva el original y registra el candidato corregido. La conformidad del
    candidato la decide el servicio de validación; aquí solo se materializa el
    candidato manteniendo el estado ``Pendiente`` hasta que el servicio confirme
    el resultado.
    """
    if item.state != QuarantineState.PENDIENTE:
        raise QuarantineError("solo un elemento Pendiente puede reinyectarse")
    if not corrected_payload:
        raise QuarantineError("la reinyección requiere contenido corregido")
    return replace(item, candidate_payload=dict(corrected_payload))


def mark_reinjected(item: QuarantineItem) -> QuarantineItem:
    """Confirma la transición a ``Reinyectado`` tras validar el candidato."""
    if item.state != QuarantineState.PENDIENTE:
        raise QuarantineError("solo un elemento Pendiente puede marcarse Reinyectado")
    return replace(item, state=QuarantineState.REINYECTADO)


def keep_pending(item: QuarantineItem, cause: QuarantineCause, corrected_payload: dict[str, object]) -> QuarantineItem:
    """Mantiene en ``Pendiente`` con causa actualizada tras un candidato no conforme."""
    if item.state != QuarantineState.PENDIENTE:
        raise QuarantineError("solo un elemento Pendiente puede actualizar su causa")
    return replace(item, cause=cause, candidate_payload=dict(corrected_payload))


def discard(item: QuarantineItem, justification: str) -> QuarantineItem:
    """Descarta un elemento ``Pendiente`` con justificación no vacía obligatoria."""
    if item.state != QuarantineState.PENDIENTE:
        raise QuarantineError("solo un elemento Pendiente puede descartarse")
    if justification is None or justification.strip() == "":
        raise QuarantineError("el descarte requiere una justificación no vacía")
    return replace(item, state=QuarantineState.DESCARTADO, discard_justification=justification.strip())


def is_reinjectable(item: QuarantineItem) -> bool:
    return item.state == QuarantineState.PENDIENTE


def is_discardable(item: QuarantineItem) -> bool:
    return item.state == QuarantineState.PENDIENTE
