"""Pruebas de contratos, validación estructural y por registro."""
from __future__ import annotations

from uuid import uuid4

from app.ingestion.models import SourceFamily
from app.validation.contracts import contract_for_family
from app.validation.models import QuarantineCause
from app.validation.records import derive_record_operation_id, validate_record
from app.validation.structural import validate_structure

CONTRACT = contract_for_family(SourceFamily.CONTRACTS_DOCUMENTS)

_HEADERS = ("ID_Contrato", "Fecha_Solicitud", "Fecha_Firma", "Fecha_Vencimiento", "Estado_Revision")


def _valid_contract_row() -> dict[str, object]:
    return {
        "ID_Contrato": "C-1",
        "Fecha_Solicitud": "2026-01-10",
        "Fecha_Firma": "2026-01-15",
        "Fecha_Vencimiento": "2026-06-30",
        "Estado_Revision": "No iniciado",
    }


# --- estructural -----------------------------------------------------------

def test_present_and_named_required_columns_pass() -> None:
    result = validate_structure(_HEADERS, [(1, 5), (2, 5)], CONTRACT)
    assert result.conforming is True
    assert result.cause is None


def test_missing_required_column_rejects_file() -> None:
    result = validate_structure(("ID_Contrato", "Fecha_Solicitud"), [(1, 2)], CONTRACT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.STRUCTURAL_INCONSISTENCY
    assert "Fecha_Vencimiento" in result.missing_columns


def test_inconsistent_row_width_rejects_file() -> None:
    result = validate_structure(_HEADERS, [(1, 5), (2, 4)], CONTRACT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.STRUCTURAL_INCONSISTENCY
    assert result.inconsistent_rows == (2,)


# --- por registro ----------------------------------------------------------

def test_valid_record_is_conforming() -> None:
    result = validate_record(_valid_contract_row(), CONTRACT)
    assert result.conforming is True


def test_missing_required_field_is_quarantined() -> None:
    row = _valid_contract_row()
    row["Fecha_Solicitud"] = ""
    result = validate_record(row, CONTRACT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.MISSING_REQUIRED_FIELD


def test_invalid_date_is_quarantined() -> None:
    row = _valid_contract_row()
    row["Fecha_Vencimiento"] = "not-a-date"
    result = validate_record(row, CONTRACT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.INVALID_DATE


def test_out_of_catalog_state_is_quarantined() -> None:
    row = _valid_contract_row()
    row["Estado_Revision"] = "Pendiente"
    result = validate_record(row, CONTRACT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.OUT_OF_CATALOG


def test_signature_before_request_date_is_quarantined() -> None:
    row = _valid_contract_row()
    row["Fecha_Firma"] = "2026-01-01"
    result = validate_record(row, CONTRACT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.DATE_ORDER_VIOLATION


def test_optional_signature_absence_is_conforming() -> None:
    row = _valid_contract_row()
    row["Fecha_Firma"] = ""
    result = validate_record(row, CONTRACT)
    assert result.conforming is True


def test_negative_litigation_amount_is_quarantined() -> None:
    contract = contract_for_family(SourceFamily.LITIGATION)
    row = {
        "ID_Litigio": "L-1",
        "Fecha_Apertura": "2026-02-01",
        "Estado": "Activo",
        "Nivel_Severidad": "alto",
        "Monto_Reclamado": "-5",
        "Estimacion_Interna": "",
    }
    result = validate_record(row, contract)
    assert result.conforming is False
    assert result.cause == QuarantineCause.NEGATIVE_AMOUNT


def test_absent_amounts_are_conforming_for_litigation() -> None:
    contract = contract_for_family(SourceFamily.LITIGATION)
    row = {
        "ID_Litigio": "L-1",
        "Fecha_Apertura": "2026-02-01",
        "Estado": "Activo",
        "Nivel_Severidad": "medio",
        "Monto_Reclamado": "",
        "Estimacion_Interna": "",
    }
    assert validate_record(row, contract).conforming is True


# --- identidad por registro ------------------------------------------------

def test_same_source_record_yields_same_child_operation_id() -> None:
    dispatch_id = uuid4()
    source_id = uuid4()
    assert derive_record_operation_id(dispatch_id, source_id) == derive_record_operation_id(dispatch_id, source_id)


def test_different_source_records_yield_different_child_ids() -> None:
    dispatch_id = uuid4()
    assert derive_record_operation_id(dispatch_id, uuid4()) != derive_record_operation_id(dispatch_id, uuid4())


def test_different_dispatch_yields_different_child_id() -> None:
    source_id = uuid4()
    assert derive_record_operation_id(uuid4(), source_id) != derive_record_operation_id(uuid4(), source_id)
