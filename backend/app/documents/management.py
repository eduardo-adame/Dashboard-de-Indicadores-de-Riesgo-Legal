"""Gestión técnica del procesamiento OCR: estado observable y reproceso."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OcrStatus:
    """Estado técnico observable de un documento procesado por OCR."""

    document_id: str
    file_name: str
    total_pages: int | None
    processed_pages: int | None
    confidence: float | None
    granularity: str | None
    state: str
    processed_at: str | None = None
    reason: str | None = None


def can_reprocess(state: str) -> bool:
    """Un documento es reprocesable salvo que ya esté en curso."""
    return state not in ("PROCESANDO", "Pendiente")


def reprocess_preserves_history(previous_runs: int) -> int:
    """El reproceso añade un intento sin descartar el historial previo."""
    return previous_runs + 1
