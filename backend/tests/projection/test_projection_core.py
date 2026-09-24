from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import MappingProxyType
from uuid import uuid4

import pytest

from app.ingestion.models import SourceFamily
from app.projection.models import ProjectionError
from app.projection.service import _targets_for, canonical_payload_sha256, derive_child_operation_id
from app.validation.models import ValidatedTabularRecord


def _record(family: SourceFamily, values: dict[str, object]) -> ValidatedTabularRecord:
    return ValidatedTabularRecord(uuid4(), family, 1, MappingProxyType(values))


def test_audit_legal_matter_without_incident_is_supported() -> None:
    targets = _targets_for(_record(SourceFamily.INTERNAL_AUDIT, {
        "ID_Asunto": "A-1", "Tipo_Asunto": "Auditoría", "Estado": "Abierto", "Fecha": "2026-01-01",
    }))
    assert len(targets) == 1
    assert targets[0].entity_type == "ASUNTO"
    assert targets[0].values["source_entity_type"] == "AUDITORIA_INTERNA"
    assert targets[0].values["source_business_id"] == "A-1"


def test_audit_incident_and_legal_matter_keep_legal_matter_provenance_independent() -> None:
    targets = _targets_for(_record(SourceFamily.INTERNAL_AUDIT, {
        "ID_Incidente": "I-1", "Fecha_Evento": "2026-01-01", "Area": "Legal", "Nivel_Severidad": "alto",
        "ID_Asunto": "A-1", "Tipo_Asunto": "Auditoría", "Estado": "Abierto", "Fecha": "2026-01-01",
    }))
    matter = next(target for target in targets if target.entity_type == "ASUNTO")
    assert matter.values["source_entity_type"] == "AUDITORIA_INTERNA"
    assert matter.values["source_business_id"] == "A-1"


def test_multi_target_snapshot_returns_one_target_definition_per_entity() -> None:
    targets = _targets_for(_record(SourceFamily.COMPLIANCE, {
        "ID_Obligacion": "O-1", "Fecha_Limite": "2026-01-01",
        "ID_Incidente": "I-1", "Fecha_Evento": "2026-01-02", "Area": "Legal", "Nivel_Severidad": "alto",
        "ID_Asunto": "A-1", "Tipo_Asunto": "Cumplimiento", "Estado": "Abierto", "Fecha": "2026-01-03",
    }))
    assert [target.entity_type for target in targets] == ["OBLIGACION", "INCIDENTE", "ASUNTO"]


@pytest.mark.parametrize(("family", "values", "entity_type"), [
    (SourceFamily.CONTRACTS_DOCUMENTS, {
        "ID_Contrato": "C-1", "Fecha_Solicitud": "2026-01-01", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado",
    }, "CONTRATO"),
    (SourceFamily.LITIGATION, {
        "ID_Litigio": "L-1", "Fecha_Apertura": "2026-01-01", "Estado": "Activo", "Nivel_Severidad": "alto",
    }, "LITIGIO"),
    (SourceFamily.COMPLIANCE, {
        "ID_Obligacion": "O-1", "Fecha_Limite": "2026-01-01",
    }, "OBLIGACION"),
    (SourceFamily.INTERNAL_AUDIT, {
        "ID_Incidente": "I-1", "Fecha_Evento": "2026-01-01", "Area": "Legal", "Nivel_Severidad": "alto",
    }, "INCIDENTE"),
])
def test_each_primary_family_maps_to_its_analytical_entity(family: SourceFamily, values: dict[str, object], entity_type: str) -> None:
    assert _targets_for(_record(family, values))[0].entity_type == entity_type


def test_partial_secondary_target_is_not_projected() -> None:
    targets = _targets_for(_record(SourceFamily.CONTRACTS_DOCUMENTS, {
        "ID_Contrato": "C-1", "Fecha_Solicitud": "2026-01-01", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado",
        "ID_Asunto": "A-1", "Tipo_Asunto": "Contrato",
    }))
    assert [target.entity_type for target in targets] == ["CONTRATO"]


def test_decimal_canonicalization_is_equivalent_and_technical_values_are_excluded() -> None:
    assert canonical_payload_sha256({"amount": Decimal("1.0"), "date": date(2026, 1, 1), "value": None}) == canonical_payload_sha256({"amount": Decimal("1.00"), "date": date(2026, 1, 1), "value": None})


def test_float_payload_is_not_silently_normalized() -> None:
    with pytest.raises(ProjectionError):
        canonical_payload_sha256({"amount": 1.5})


def test_child_operation_ids_are_stable_and_distinct_per_target() -> None:
    parent, source = uuid4(), uuid4()
    assert derive_child_operation_id(parent, source, "CONTRATO", "C-1") == derive_child_operation_id(parent, source, "CONTRATO", "C-1")
    assert derive_child_operation_id(parent, source, "CONTRATO", "C-1") != derive_child_operation_id(parent, source, "ASUNTO", "A-1")
