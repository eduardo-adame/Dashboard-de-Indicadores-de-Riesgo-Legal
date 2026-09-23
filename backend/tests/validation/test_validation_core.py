"""Pruebas de contratos, validación estructural y por registro."""
from __future__ import annotations

from uuid import uuid4

from app.ingestion.models import SourceFamily
from app.validation.contracts import (
    AUDIT_INCIDENT,
    AUDIT_LEGAL_MATTER,
    contract_for_family,
    contracts_for_family,
)
from app.validation.models import QuarantineCause
from app.validation.records import derive_record_operation_id, validate_record, validate_record_for_family
from app.validation.structural import validate_structure, validate_structure_for_family

CONTRACT = contract_for_family(SourceFamily.CONTRACTS_DOCUMENTS)

_HEADERS = ("ID_Contrato", "Fecha_Solicitud", "Fecha_Firma", "Fecha_Vencimiento", "Estado_Revision")
_AUDIT_INCIDENT_HEADERS = ("ID_Incidente", "Fecha_Evento", "Area", "Nivel_Severidad")
_AUDIT_LEGAL_MATTER_HEADERS = ("ID_Asunto", "Tipo_Asunto", "Estado", "Fecha")


def _valid_contract_row() -> dict[str, object]:
    return {
        "ID_Contrato": "C-1",
        "Fecha_Solicitud": "2026-01-10",
        "Fecha_Firma": "2026-01-15",
        "Fecha_Vencimiento": "2026-06-30",
        "Estado_Revision": "No iniciado",
    }


def _valid_audit_incident() -> dict[str, object]:
    return {
        "ID_Incidente": "I-1",
        "Fecha_Evento": "2026-03-01",
        "Area": "Cumplimiento",
        "Nivel_Severidad": "alto",
    }


def _valid_audit_legal_matter() -> dict[str, object]:
    return {
        "ID_Asunto": "A-1",
        "Tipo_Asunto": "Auditoría",
        "Estado": "Abierto",
        "Fecha": "2026-03-01",
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


def test_audit_legal_matter_headers_are_structurally_valid() -> None:
    result = validate_structure_for_family(
        _AUDIT_LEGAL_MATTER_HEADERS,
        [(1, len(_AUDIT_LEGAL_MATTER_HEADERS))],
        SourceFamily.INTERNAL_AUDIT,
    )
    assert result.conforming is True


def test_legacy_contract_for_internal_audit_remains_incident_contract() -> None:
    assert contract_for_family(SourceFamily.INTERNAL_AUDIT) is AUDIT_INCIDENT


def test_internal_audit_composite_contracts_have_stable_order() -> None:
    assert contracts_for_family(SourceFamily.INTERNAL_AUDIT) == (
        AUDIT_INCIDENT,
        AUDIT_LEGAL_MATTER,
    )


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


# --- contratos compuestos de auditoría ------------------------------------

def test_audit_incident_contract_is_valid_when_complete() -> None:
    assert validate_record_for_family(
        _valid_audit_incident(), SourceFamily.INTERNAL_AUDIT
    ).conforming is True


def test_audit_legal_matter_contract_is_valid_without_incident_fields() -> None:
    assert validate_record_for_family(
        _valid_audit_legal_matter(), SourceFamily.INTERNAL_AUDIT
    ).conforming is True


def test_audit_legal_matter_does_not_require_id_incidente() -> None:
    values = _valid_audit_legal_matter()
    values["ID_Incidente"] = ""
    assert validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT).conforming is True


def test_audit_row_with_both_incident_and_legal_matter_contracts_is_supported() -> None:
    values = _valid_audit_incident() | _valid_audit_legal_matter()
    assert validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT).conforming is True


def test_audit_legal_matter_invalid_when_required_matter_field_missing() -> None:
    values = _valid_audit_legal_matter()
    del values["Fecha"]
    result = validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.MISSING_REQUIRED_FIELD


def test_audit_legal_matter_invalid_tipo_asunto() -> None:
    values = _valid_audit_legal_matter()
    values["Tipo_Asunto"] = "Otro"
    result = validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.OUT_OF_CATALOG


def test_empty_audit_row_is_not_conforming() -> None:
    result = validate_record_for_family({}, SourceFamily.INTERNAL_AUDIT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.MISSING_REQUIRED_FIELD


def test_partial_incident_with_valid_legal_matter_is_not_conforming() -> None:
    values = _valid_audit_legal_matter()
    values["ID_Incidente"] = "I-1"
    result = validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.MISSING_REQUIRED_FIELD
    assert result.field_name == "Fecha_Evento"


def test_valid_incident_with_invalid_legal_matter_is_not_conforming() -> None:
    values = _valid_audit_incident() | _valid_audit_legal_matter()
    values["Tipo_Asunto"] = "Otro"
    result = validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT)
    assert result.conforming is False
    assert result.cause == QuarantineCause.OUT_OF_CATALOG
    assert result.field_name == "Tipo_Asunto"


def test_audit_multiple_invalid_contracts_have_deterministic_primary_violation() -> None:
    values = _valid_audit_incident() | _valid_audit_legal_matter()
    values["Nivel_Severidad"] = "crítico"
    values["Tipo_Asunto"] = "Otro"
    first = validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT)
    second = validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT)
    assert first == second
    assert first.cause == QuarantineCause.OUT_OF_CATALOG
    assert first.field_name == "Nivel_Severidad"


def test_extra_unpopulated_audit_columns_do_not_create_a_target() -> None:
    values = _valid_audit_legal_matter() | {
        "ID_Incidente": "",
        "Fecha_Evento": "",
        "Area": "",
        "Nivel_Severidad": "",
    }
    assert validate_record_for_family(values, SourceFamily.INTERNAL_AUDIT).conforming is True


def test_non_audit_family_contracts_are_unchanged() -> None:
    litigation = contract_for_family(SourceFamily.LITIGATION)
    assert contracts_for_family(SourceFamily.LITIGATION) == (litigation,)
    assert validate_record_for_family(
        {
            "ID_Litigio": "L-1",
            "Fecha_Apertura": "2026-02-01",
            "Estado": "Activo",
            "Nivel_Severidad": "medio",
        },
        SourceFamily.LITIGATION,
    ).conforming is True


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
