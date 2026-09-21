"""Motor de OCR basado en Tesseract, cargado de forma perezosa.

El motor interpreta una imagen de página ya rasterizada y devuelve el texto
reconocido con su confianza normalizada. La rasterización de PDF y la selección
de páginas son responsabilidad de la capa de procesamiento documental.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.ocr.confidence import aggregate_confidence, classify_state


class OcrError(Exception):
    """Error controlado del procesamiento OCR."""


class OcrUnavailableError(OcrError):
    """El motor OCR no está disponible en este proceso."""


class OcrState(StrEnum):
    PENDIENTE = "Pendiente"
    EXITOSO = "Exitoso"
    RECHAZADO_BAJA_CONFIANZA = "Rechazado por baja confianza"


@dataclass(frozen=True)
class OcrPageResult:
    page_number: int
    text: str
    confidence: float | None
    granularity: str | None = None


class OcrEngine(Protocol):
    def recognize(self, image: object, *, page_number: int) -> OcrPageResult: ...


class TesseractOcrEngine:
    """Envoltura de Tesseract para texto impreso en español."""

    def __init__(self, language: str = "spa") -> None:
        self._language = language

    @staticmethod
    def is_available() -> bool:
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            return False
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
        except Exception:  # noqa: BLE001 - binario externo ausente
            return False
        return True

    def recognize(self, image: object, *, page_number: int) -> OcrPageResult:
        try:
            import pytesseract
            from pytesseract import Output
        except ImportError as exc:  # pragma: no cover - dependencia externa
            raise OcrUnavailableError(f"OCR no disponible: {exc.__class__.__name__}") from exc

        try:
            data = pytesseract.image_to_data(
                image,
                lang=self._language,
                output_type=Output.DICT,
            )
        except Exception as exc:  # noqa: BLE001 - frontera de dependencia externa
            raise OcrError(f"{exc.__class__.__name__}: fallo de reconocimiento") from exc

        confidences: list[float] = []
        words: list[str] = []
        for text, confidence in zip(data.get("text", []), data.get("conf", [])):
            if text and text.strip():
                words.append(text)
                try:
                    value = float(confidence)
                except (TypeError, ValueError):
                    continue
                if value >= 0:
                    confidences.append(value / 100.0)

        aggregate = aggregate_confidence(confidences)
        return OcrPageResult(
            page_number=page_number,
            text=" ".join(words),
            confidence=aggregate,
            granularity="WORD",
        )


def state_for(confidence: float | None) -> OcrState:
    """Traduce la confianza agregada a estado técnico OCR."""
    return OcrState(classify_state(confidence))
