"""Agregación y clasificación de la confianza del reconocimiento OCR.

La confianza se normaliza en el rango 0.00–1.00. La confianza agregada es el
promedio aritmético de los valores válidos disponibles; la ausencia de muestras no
se interpreta como confianza cero.
"""
from __future__ import annotations

from collections.abc import Iterable

# Umbral de aceptación del texto reconocido.
LOW_CONFIDENCE_THRESHOLD = 0.80


def _valid(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 1:
        return None
    return number


def aggregate_confidence(samples: Iterable[object]) -> float | None:
    """Promedio aritmético de los valores de confianza válidos.

    Devuelve ``None`` cuando no hay ningún valor válido (ausencia, nunca cero).
    """
    valid = [number for number in (_valid(sample) for sample in samples) if number is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def classify_state(confidence: float | None) -> str:
    """Clasifica el estado técnico del OCR a partir de la confianza agregada."""
    if confidence is None:
        return "Pendiente"
    if confidence >= LOW_CONFIDENCE_THRESHOLD:
        return "Exitoso"
    return "Rechazado por baja confianza"
