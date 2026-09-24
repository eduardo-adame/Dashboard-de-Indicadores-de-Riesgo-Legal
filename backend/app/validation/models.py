"""Tipos y contratos internos del módulo de validación."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from app.ingestion.models import SourceFamily


class FieldType(StrEnum):
    TEXT = "TEXT"
    DATE = "DATE"
    DECIMAL_NON_NEGATIVE = "DECIMAL_NON_NEGATIVE"
    CATALOG = "CATALOG"


class QuarantineCause(StrEnum):
    """Causas seguras de cuarentena o rechazo técnico."""

    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    INVALID_TYPE = "INVALID_TYPE"
    INVALID_DATE = "INVALID_DATE"
    OUT_OF_CATALOG = "OUT_OF_CATALOG"
    NEGATIVE_AMOUNT = "NEGATIVE_AMOUNT"
    DATE_ORDER_VIOLATION = "DATE_ORDER_VIOLATION"
    STRUCTURAL_INCONSISTENCY = "STRUCTURAL_INCONSISTENCY"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    TECHNICAL_READ_FAILURE = "TECHNICAL_READ_FAILURE"
    OTHER_CAUSE = "OTHER_CAUSE"


class QuarantineState(StrEnum):
    """Estados funcionales del elemento en cuarentena (coinciden con el esquema)."""

    PENDIENTE = "Pendiente"
    REINYECTADO = "Reinyectado"
    DESCARTADO = "Descartado"


class RecordDisposition(StrEnum):
    CONFORME = "CONFORME"
    CUARENTENA = "CUARENTENA"


class ValidationInvocationError(RuntimeError):
    """La invocación no coincide con la procedencia persistida."""


@dataclass(frozen=True)
class FieldSpec:
    """Especificación de un campo del contrato de datos."""

    name: str
    field_type: FieldType
    required: bool
    catalog: frozenset[str] | None = None
    # Nombre del campo fecha que este no puede preceder (p. ej. Fecha_Firma >= Fecha_Solicitud).
    date_not_before: str | None = None


@dataclass(frozen=True)
class DataContract:
    """Contrato de datos de una familia del MVP."""

    family: SourceFamily
    stable_id: str
    required_columns: tuple[str, ...]
    fields: tuple[FieldSpec, ...]

    def field(self, name: str) -> FieldSpec | None:
        for spec in self.fields:
            if spec.name == name:
                return spec
        return None


@dataclass(frozen=True)
class StructuralValidation:
    """Resultado de la validación estructural de un conjunto extraído."""

    conforming: bool
    cause: QuarantineCause | None
    missing_columns: tuple[str, ...] = ()
    inconsistent_rows: tuple[int, ...] = ()


@dataclass(frozen=True)
class RecordValidation:
    """Resultado de la validación de un registro individual."""

    conforming: bool
    cause: QuarantineCause | None
    field_name: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    """Resultado agregado de validar un archivo tabular."""

    file_id: str
    family: SourceFamily
    structural: StructuralValidation
    conforming_positions: tuple[int, ...]
    quarantined: tuple[tuple[int, QuarantineCause], ...]
    validated_records: tuple["ValidatedTabularRecord", ...] = field(
        default=(), compare=False, repr=False
    )

    @property
    def file_rejected(self) -> bool:
        return not self.structural.conforming


@dataclass(frozen=True)
class ValidatedTabularRecord:
    """Snapshot inmutable del registro exacto que superó la validación."""

    source_record_id: UUID
    family: SourceFamily
    position: int
    values_by_name: Mapping[str, object]
