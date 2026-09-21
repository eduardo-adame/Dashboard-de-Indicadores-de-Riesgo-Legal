"""Detección técnica de formatos y límites de contenedores."""
from __future__ import annotations

from pathlib import Path
import zipfile

from pypdf import PdfReader

from app.ingestion.csv_policy import CsvStructureError, determine_delimiter
from app.ingestion.models import DetectionResult, ExchangeFormat, IngestionLimits


def _archive_kind(path: Path, limits: IngestionLimits) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > limits.max_archive_entries:
                return "ARCHIVE_LIMIT_EXCEEDED"
            expanded = 0
            for entry in entries:
                expanded += entry.file_size
                if entry.file_size > limits.max_archive_entry_bytes or expanded > limits.max_archive_uncompressed_bytes:
                    return "ARCHIVE_LIMIT_EXCEEDED"
                compressed = max(entry.compress_size, 1)
                if entry.file_size / compressed > limits.max_compression_ratio:
                    return "ARCHIVE_COMPRESSION_RATIO_EXCEEDED"
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return "CORRUPT_ARCHIVE"
    if "[Content_Types].xml" not in names:
        return "UNSUPPORTED"
    if "xl/workbook.xml" in names:
        return ExchangeFormat.XLSX.value
    if "word/document.xml" in names:
        return ExchangeFormat.DOCX.value
    return "UNSUPPORTED"


def _csv_kind(path: Path, limits: IngestionLimits) -> str:
    with path.open("rb") as source:
        sample = source.read(limits.csv_sample_bytes)
    if b"\x00" in sample:
        return "BINARY_CONTENT"
    try:
        text = sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "UNSUPPORTED_ENCODING"
    if not text:
        return ExchangeFormat.CSV.value
    try:
        determine_delimiter(text)
    except CsvStructureError as exc:
        return exc.cause_code
    return ExchangeFormat.CSV.value


def detect_format(path: Path, original_name: str, limits: IngestionLimits) -> DetectionResult:
    declared = Path(original_name).suffix.lower()
    expected = {
        ".csv": ExchangeFormat.CSV,
        ".xlsx": ExchangeFormat.XLSX,
        ".pdf": ExchangeFormat.PDF,
        ".docx": ExchangeFormat.DOCX,
    }.get(declared)
    with path.open("rb") as source:
        prefix = source.read(8)
    detected: str
    if prefix.startswith(b"%PDF-"):
        try:
            reader = PdfReader(str(path), strict=True)
            if reader.is_encrypted:
                detected = "PROTECTED_PDF"
            else:
                len(reader.pages)
                detected = ExchangeFormat.PDF.value
        except Exception:
            detected = "CORRUPT_PDF"
    elif zipfile.is_zipfile(path):
        detected = _archive_kind(path, limits)
    else:
        detected = _csv_kind(path, limits)

    actual = ExchangeFormat(detected) if detected in ExchangeFormat._value2member_map_ else None
    if expected is None:
        return DetectionResult(declared, detected, None, False, False, "UNSUPPORTED_EXTENSION")
    if actual is None:
        cause = detected if detected != "UNSUPPORTED" else "UNSUPPORTED_FORMAT"
        return DetectionResult(declared, detected, expected, True, False, cause)
    if expected != actual:
        return DetectionResult(declared, detected, actual, True, False, "FORMAT_MISMATCH")
    return DetectionResult(declared, detected, actual, True, True)
