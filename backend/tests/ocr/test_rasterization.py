"""Pruebas del rasterizador PDF (sin requerir Poppler en el host)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.ocr.rasterization import RasterizationError, rasterize_pdf


def test_dpi_out_of_range_is_rejected(tmp_path: Path) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(RasterizationError):
        next(rasterize_pdf(pdf, dpi=10_000))
