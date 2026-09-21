"""Consolidación del texto nativo y del texto reconocido por OCR.

Produce el texto íntegro de un documento procesable conservando el mapeo de
páginas. El texto OCR solo se incorpora cuando su estado técnico es ``Exitoso``;
el texto rechazado por baja confianza se excluye y se conserva únicamente como
trazabilidad.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.ocr.confidence import LOW_CONFIDENCE_THRESHOLD


@dataclass(frozen=True)
class ConsolidatedText:
    text: str
    page_map: tuple[tuple[int, int, int], ...]  # (page_number, start_char, end_char)


@dataclass(frozen=True)
class NativePage:
    page_number: int
    native_text: str
    requires_ocr: bool


def consolidate(
    pages: Sequence[NativePage],
    ocr_text_by_page: dict[int, tuple[str, float | None]],
) -> ConsolidatedText:
    """Combina texto nativo y OCR ``Exitoso`` preservando el orden de páginas.

    ``ocr_text_by_page`` asocia número de página con ``(texto, confianza)``.
    Una página que requiere OCR solo aporta texto cuando su confianza es
    suficiente; en caso contrario se omite del texto consolidado.
    """
    parts: list[str] = []
    page_map: list[tuple[int, int, int]] = []
    cursor = 0
    for page in pages:
        text = page.native_text
        if page.requires_ocr:
            ocr = ocr_text_by_page.get(page.page_number)
            if ocr is None:
                continue
            ocr_text, confidence = ocr
            if confidence is None or confidence < LOW_CONFIDENCE_THRESHOLD:
                continue
            text = ocr_text
        if not text:
            continue
        if parts:
            parts.append("\n\n")
            cursor += 2
        start = cursor
        parts.append(text)
        cursor += len(text)
        page_map.append((page.page_number, start, cursor))
    return ConsolidatedText(text="".join(parts), page_map=tuple(page_map))
