"""Pruebas de la gestión técnica del OCR."""
from __future__ import annotations

from app.documents.management import can_reprocess, reprocess_preserves_history


def test_terminal_states_are_reprocessable() -> None:
    assert can_reprocess("Exitoso") is True
    assert can_reprocess("Rechazado por baja confianza") is True


def test_in_progress_state_is_not_reprocessable() -> None:
    assert can_reprocess("PROCESANDO") is False
    assert can_reprocess("Pendiente") is False


def test_reprocess_adds_attempt_without_dropping_history() -> None:
    assert reprocess_preserves_history(0) == 1
    assert reprocess_preserves_history(3) == 4
