"""Validación de campos, tipos y reglas de dominio por registro individual.

Opera sobre valores ya extraídos y nombrados por encabezado. No vuelve a inferir
delimitadores ni a parsear el archivo: aplica el contrato de datos del SRS.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid5

from app.ingestion.models import SourceFamily
from app.validation.contracts import contracts_for_family
from app.validation.models import (
    DataContract,
    FieldSpec,
    FieldType,
    QuarantineCause,
    RecordValidation,
)

# Namespace fijo para derivar identidades hijas deterministas a partir de la
# identidad estable del despacho y la identidad del registro fuente.
_RECORD_NAMESPACE = UUID("6f8f0f1e-2b0a-4c2f-9a3d-5c7e1b9d4a10")


def derive_record_operation_id(dispatch_operation_id: UUID, source_record_id: UUID) -> UUID:
    """Identidad funcional estable por registro.

    Determinista: el mismo despacho y el mismo registro producen siempre el mismo
    identificador, de modo que un reintento no crea una segunda aplicación.
    """
    return uuid5(_RECORD_NAMESPACE, f"{dispatch_operation_id}:{source_record_id}")


def _is_absent(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def validate_record(values_by_name: Mapping[str, object], contract: DataContract) -> RecordValidation:
    """Valida un registro contra el contrato de datos de su familia."""
    # 1. Campos obligatorios y tipos.
    for spec in contract.fields:
        raw = values_by_name.get(spec.name)
        if _is_absent(raw):
            if spec.required:
                return RecordValidation(False, QuarantineCause.MISSING_REQUIRED_FIELD, spec.name)
            continue
        result = _validate_present_field(raw, spec)
        if result is not None:
            return result

    # 2. Reglas de orden entre fechas.
    for spec in contract.fields:
        if spec.date_not_before is None:
            continue
        current_raw = values_by_name.get(spec.name)
        reference_raw = values_by_name.get(spec.date_not_before)
        if _is_absent(current_raw) or _is_absent(reference_raw):
            continue
        current = _parse_date(current_raw)
        reference = _parse_date(reference_raw)
        if current is None or reference is None:
            continue
        if current < reference:
            return RecordValidation(False, QuarantineCause.DATE_ORDER_VIOLATION, spec.name)

    return RecordValidation(True, None, None)


def validate_record_for_family(
    values_by_name: Mapping[str, object],
    family: SourceFamily,
) -> RecordValidation:
    """Valida todos los contratos explícitamente presentes de una familia."""
    contracts = contracts_for_family(family)
    if len(contracts) == 1:
        return validate_record(values_by_name, contracts[0])

    present_contracts = [
        contract
        for contract in contracts
        if any(not _is_absent(values_by_name.get(spec.name)) for spec in contract.fields)
    ]
    if not present_contracts:
        return validate_record(values_by_name, contracts[0])

    for contract in present_contracts:
        outcome = validate_record(values_by_name, contract)
        if not outcome.conforming:
            return outcome
    return RecordValidation(True, None, None)


def _validate_present_field(raw: object, spec: FieldSpec) -> RecordValidation | None:
    if spec.field_type == FieldType.TEXT:
        if str(raw).strip() == "":
            return RecordValidation(False, QuarantineCause.MISSING_REQUIRED_FIELD, spec.name)
        return None
    if spec.field_type == FieldType.DATE:
        if _parse_date(raw) is None:
            return RecordValidation(False, QuarantineCause.INVALID_DATE, spec.name)
        return None
    if spec.field_type == FieldType.DECIMAL_NON_NEGATIVE:
        parsed = _parse_decimal(raw)
        if parsed is None:
            return RecordValidation(False, QuarantineCause.INVALID_TYPE, spec.name)
        if parsed < 0:
            return RecordValidation(False, QuarantineCause.NEGATIVE_AMOUNT, spec.name)
        return None
    if spec.field_type == FieldType.CATALOG:
        if spec.catalog is not None and str(raw).strip() not in spec.catalog:
            return RecordValidation(False, QuarantineCause.OUT_OF_CATALOG, spec.name)
        return None
    return RecordValidation(False, QuarantineCause.INVALID_TYPE, spec.name)
