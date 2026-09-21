"""Pruebas de agregación y clasificación de confianza OCR."""
from __future__ import annotations

import pytest

from app.ocr.confidence import LOW_CONFIDENCE_THRESHOLD, aggregate_confidence, classify_state


def test_aggregate_is_arithmetic_mean_of_valid_samples() -> None:
    result = aggregate_confidence([0.9, 0.7, 0.5])
    assert result == pytest.approx(0.7)


def test_invalid_samples_are_ignored() -> None:
    assert aggregate_confidence([0.8, None, -1, 2, "x"]) == 0.8


def test_no_valid_samples_returns_none_not_zero() -> None:
    assert aggregate_confidence([]) is None
    assert aggregate_confidence([None, "x"]) is None


def test_state_boundary_at_threshold() -> None:
    assert classify_state(LOW_CONFIDENCE_THRESHOLD) == "Exitoso"
    assert classify_state(0.79999) == "Rechazado por baja confianza"


def test_absent_confidence_is_pending() -> None:
    assert classify_state(None) == "Pendiente"
