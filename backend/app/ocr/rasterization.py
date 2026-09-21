"""Rasterización de páginas PDF a imagen para el reconocimiento OCR.

Convierte una página PDF a una imagen decodificada, imponiendo un límite de
dimensiones. El límite se evalúa sobre los píxeles de la imagen resultante, no
sobre el tamaño del archivo comprimido.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.config import get_settings


class RasterizationError(Exception):
    """Error de rasterización de una página PDF."""


class RasterizationUnavailableError(RasterizationError):
    """El rasterizador no está disponible en este proceso."""


@dataclass(frozen=True)
class RasterizedPage:
    page_number: int
    width: int
    height: int
    image: object


def _pixel_limit() -> int:
    return get_settings().ocr_max_image_pixels


def rasterize_pdf(
    pdf_path: Path,
    *,
    dpi: int | None = None,
    first_page: int | None = None,
    last_page: int | None = None,
) -> Iterator[RasterizedPage]:
    """Rasteriza páginas del PDF, una a una, sin retenerlas todas en memoria."""
    try:
        from pdf2image import convert_from_path
    except ImportError as exc:  # pragma: no cover - dependencia externa
        raise RasterizationUnavailableError(f"rasterizador no disponible: {exc.__class__.__name__}") from exc

    settings = get_settings()
    effective_dpi = dpi or settings.ocr_dpi
    if not (settings.ocr_min_dpi <= effective_dpi <= settings.ocr_max_dpi):
        raise RasterizationError(f"DPI fuera de rango: {effective_dpi}")

    limit = _pixel_limit()
    start = first_page or 1
    end = last_page
    page_number = start
    while True:
        try:
            batch = convert_from_path(
                str(pdf_path),
                dpi=effective_dpi,
                first_page=page_number,
                last_page=page_number if end is None else min(page_number, end),
                fmt="png",
            )
        except Exception as exc:  # noqa: BLE001 - frontera de dependencia externa
            raise RasterizationError(f"{exc.__class__.__name__}: fallo de rasterización") from exc
        if not batch:
            break
        image = batch[0]
        width, height = image.size
        if width * height > limit:
            raise RasterizationError(f"imagen excede el límite de píxeles: {width}x{height}")
        yield RasterizedPage(page_number=page_number, width=width, height=height, image=image)
        if end is not None and page_number >= end:
            break
        page_number += 1
