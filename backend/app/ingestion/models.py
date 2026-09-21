"""Contratos internos para recepción y extracción de archivos."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID


class IngestionError(Exception):
    """Error seguro del pipeline de ingesta."""

    cause_code = "INGESTION_ERROR"


class RejectedFileError(IngestionError):
    """El archivo no puede continuar al parser normal."""

    def __init__(self, cause_code: str) -> None:
        super().__init__("El archivo no pudo aceptarse para procesamiento")
        self.cause_code = cause_code


class ResourceLimitExceeded(RejectedFileError):
    """The stream was stopped before a complete content hash existed."""

    def __init__(self, observed_byte_size: int, original_name: str) -> None:
        super().__init__("RESOURCE_LIMIT_EXCEEDED")
        self.observed_byte_size = observed_byte_size
        self.original_name = original_name


class IdempotencyConflictError(IngestionError):
    cause_code = "IDEMPOTENCY_CONFLICT"


class SourceFamily(StrEnum):
    CONTRACTS_DOCUMENTS = "CONTRATOS_DOCUMENTOS"
    LITIGATION = "LITIGIOS"
    COMPLIANCE = "CUMPLIMIENTO"
    INTERNAL_AUDIT = "AUDITORIA_INTERNA"


class ExchangeFormat(StrEnum):
    CSV = "CSV"
    XLSX = "XLSX"
    PDF = "PDF"
    DOCX = "DOCX"


class RoutingTarget(StrEnum):
    VALIDATION = "VALIDATION"
    DOCUMENT = "DOCUMENT"
    NONE = "NONE"


CONTROLLED_LOCATIONS: dict[str, SourceFamily] = {
    "contracts-documents": SourceFamily.CONTRACTS_DOCUMENTS,
    "litigation": SourceFamily.LITIGATION,
    "compliance": SourceFamily.COMPLIANCE,
    "internal-audit": SourceFamily.INTERNAL_AUDIT,
}


@dataclass(frozen=True)
class IngestionLimits:
    max_file_bytes: int
    max_archive_entries: int
    max_archive_uncompressed_bytes: int
    max_archive_entry_bytes: int
    max_compression_ratio: float
    csv_sample_bytes: int
    stream_chunk_bytes: int
    max_tabular_rows: int
    max_tabular_columns: int
    max_tabular_cells: int


@dataclass(frozen=True)
class DetectionResult:
    declared_extension: str
    detected_format: str
    exchange_format: ExchangeFormat | None
    supported: bool
    accepted: bool
    safe_cause_code: str | None = None


@dataclass(frozen=True)
class ExtractedRow:
    sheet: str | None
    row_number: int
    values: tuple[Any, ...]


@dataclass(frozen=True)
class ExtractedRecordSet:
    headers: tuple[Any, ...]
    rows: tuple[ExtractedRow, ...]
    sheets: tuple[str, ...]
    sheet_headers: tuple[tuple[str, tuple[Any, ...]], ...] = ()


@dataclass(frozen=True)
class DocumentPage:
    page_number: int
    native_text: str
    requires_ocr: bool


@dataclass(frozen=True)
class DocumentCandidate:
    pages: tuple[DocumentPage, ...]
    native_text: str
    processing_state: str


@dataclass(frozen=True)
class StagedFile:
    path: Path
    locator: str
    byte_size: int
    sha256: bytes
    original_name: str


@dataclass(frozen=True)
class IngestionResult:
    file_id: UUID
    operation_id: UUID
    correlation_id: UUID
    state: str
    format: str | None
    family: SourceFamily
    routing_target: RoutingTarget
    safe_cause_code: str | None
    idempotent: bool = False
    records: ExtractedRecordSet | None = None
    document: DocumentCandidate | None = None
