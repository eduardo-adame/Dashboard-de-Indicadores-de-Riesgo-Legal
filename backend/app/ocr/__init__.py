"""Reconocimiento óptico de caracteres y evaluación de confianza."""
from app.ocr.confidence import (
    LOW_CONFIDENCE_THRESHOLD,
    aggregate_confidence,
    classify_state,
)
from app.ocr.consolidation import ConsolidatedText, NativePage, consolidate
from app.ocr.engine import (
    OcrEngine,
    OcrError,
    OcrPageResult,
    OcrState,
    OcrUnavailableError,
    TesseractOcrEngine,
)
from app.ocr.rasterization import (
    RasterizationError,
    RasterizationUnavailableError,
    RasterizedPage,
    rasterize_pdf,
)

__all__ = (
    "LOW_CONFIDENCE_THRESHOLD",
    "ConsolidatedText",
    "NativePage",
    "OcrEngine",
    "OcrError",
    "OcrPageResult",
    "OcrState",
    "OcrUnavailableError",
    "RasterizationError",
    "RasterizationUnavailableError",
    "RasterizedPage",
    "TesseractOcrEngine",
    "aggregate_confidence",
    "classify_state",
    "consolidate",
    "rasterize_pdf",
)
