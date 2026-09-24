"""Pruebas del servicio de validación y cuarentena con repositorio en memoria."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest

from app.ingestion.models import SourceFamily
from app.security.models import AuthenticatedPrincipal
from app.validation.models import (
    QuarantineCause,
    QuarantineState,
    StructuralValidation,
    ValidatedTabularRecord,
    ValidationInvocationError,
    ValidationResult,
)
from app.validation.quarantine import QuarantineError, QuarantineItem
from app.validation.service import TabularRecord, ValidationContext, ValidationService


class InMemoryValidationRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, QuarantineItem] = {}
        self.transitions: list[object] = []
        self.audit_events: list[dict] = []
        self.family = SourceFamily.CONTRACTS_DOCUMENTS
        self.bindings: dict[UUID, tuple[UUID, int]] = {}
        self.reject_bindings = False

    @contextmanager
    def transaction(self):
        yield object()

    def find_quarantine_by_operation(self, connection, operation_id: UUID):
        for item in self.items.values():
            if item.operation_id == operation_id:
                return item
        return None

    def insert_quarantine(self, connection, *, file_id, source_record_id, cause, operation_id, correlation_id, original_payload):
        item = QuarantineItem(
            id=uuid4(),
            ingest_file_id=file_id,
            source_record_id=source_record_id,
            cause=cause or QuarantineCause.OTHER_CAUSE,
            state=QuarantineState.PENDIENTE,
            operation_id=operation_id,
            correlation_id=correlation_id,
            original_payload=original_payload,
        )
        self.items[item.id] = item
        return item.id

    def load_quarantine(self, connection, item_id: UUID, *, for_update: bool = False):
        return self.items.get(item_id)

    def update_quarantine(self, connection, item: QuarantineItem) -> None:
        self.items[item.id] = item

    def insert_transition(self, connection, transition) -> None:
        self.transitions.append(transition)

    def family_for_quarantine(self, connection, item: QuarantineItem) -> SourceFamily:
        return self.family

    def family_for_file(self, connection, file_id: UUID) -> SourceFamily:
        return self.family

    def source_record_matches(self, connection, *, source_record_id, file_id, position) -> bool:
        if self.reject_bindings:
            return False
        self.bindings[source_record_id] = (file_id, position)
        return True

    def provenance_for_quarantine(self, connection, item: QuarantineItem):
        from app.validation.repository import SourceRecordProvenance

        file_id, position = self.bindings[item.source_record_id]
        return SourceRecordProvenance(item.source_record_id, file_id, position, self.family)

    def write_audit_event(self, connection, **kwargs) -> None:
        self.audit_events.append(kwargs)


class AllowingSecurity:
    def revalidate_functional_access(self, connection, principal, capability):
        return principal


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), "analyst", 1, frozenset({"ANALISTA"}), frozenset({"ingest.execute"}))


def _context() -> ValidationContext:
    return ValidationContext(operation_id=uuid4(), correlation_id=uuid4(), actor=_principal())


_HEADERS = ("ID_Contrato", "Fecha_Solicitud", "Fecha_Firma", "Fecha_Vencimiento", "Estado_Revision")
_AUDIT_LEGAL_MATTER_HEADERS = ("ID_Asunto", "Tipo_Asunto", "Estado", "Fecha")


def _record(position: int, *, valid: bool) -> TabularRecord:
    values = {
        "ID_Contrato": f"C-{position}",
        "Fecha_Solicitud": "2026-01-10",
        "Fecha_Firma": "2026-01-15",
        "Fecha_Vencimiento": "2026-06-30",
        "Estado_Revision": "No iniciado" if valid else "Pendiente",
    }
    return TabularRecord(position=position, source_record_id=uuid4(), values_by_name=values, width=5)


def _audit_legal_matter_record(position: int, *, valid: bool) -> TabularRecord:
    return TabularRecord(
        position=position,
        source_record_id=uuid4(),
        values_by_name={
            "ID_Asunto": f"A-{position}",
            "Tipo_Asunto": "Auditoría" if valid else "Otro",
            "Estado": "Abierto",
            "Fecha": "2026-03-01",
        },
        width=4,
    )


def _service(repository: InMemoryValidationRepository) -> ValidationService:
    return ValidationService(repository, AllowingSecurity())


def test_n_invalid_records_create_n_quarantine_items() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    records = (_record(1, valid=False), _record(2, valid=False), _record(3, valid=True))
    result = service.validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=_context()
    )
    assert len(result.quarantined) == 2
    assert len(repository.items) == 2
    assert result.conforming_positions == (3,)


def test_retry_does_not_create_additional_quarantine_items() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    records = (_record(1, valid=False), _record(2, valid=False))
    context = _context()
    service.validate(file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=context)
    assert len(repository.items) == 2
    service.validate(file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS, records=records, context=context)
    assert len(repository.items) == 2


def test_structural_rejection_creates_no_quarantine_items() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    result = service.validate(
        file_id=uuid4(),
        family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=("ID_Contrato",),
        records=(_record(1, valid=True),),
        context=_context(),
    )
    assert result.file_rejected is True
    assert not repository.items
    assert result.validated_records == ()
    assert result.conforming_positions == ()


def test_reinject_valid_correction_moves_to_reinjected() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
        records=(_record(1, valid=False),), context=context,
    )
    item = next(iter(repository.items.values()))
    corrected = {
        "ID_Contrato": "C-1",
        "Fecha_Solicitud": "2026-01-10",
        "Fecha_Firma": "2026-01-15",
        "Fecha_Vencimiento": "2026-06-30",
        "Estado_Revision": "No iniciado",
    }
    updated = service.reinject(item_id=item.id, corrected_payload=corrected, context=context)
    assert updated.state == QuarantineState.REINYECTADO
    assert updated.original_payload is not None
    assert updated.candidate_payload == corrected


def test_reinject_invalid_correction_keeps_pending() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
        records=(_record(1, valid=False),), context=context,
    )
    item = next(iter(repository.items.values()))
    updated = service.reinject(item_id=item.id, corrected_payload=item.original_payload, context=context)
    assert updated.state == QuarantineState.PENDIENTE


def test_reinjection_pending_has_no_validated_snapshot() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
        records=(_record(1, valid=False),), context=context,
    )
    item = next(iter(repository.items.values()))
    result = service.reinject_with_validated_record(
        item_id=item.id, corrected_payload=item.original_payload or {}, context=context
    )
    assert result.quarantine_item.state == QuarantineState.PENDIENTE
    assert result.validated_record is None


def test_discard_requires_non_empty_justification() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
        records=(_record(1, valid=False),), context=context,
    )
    item = next(iter(repository.items.values()))
    with pytest.raises(QuarantineError):
        service.discard(item_id=item.id, justification="  ", context=context)
    updated = service.discard(item_id=item.id, justification="duplicado", context=context)
    assert updated.state == QuarantineState.DESCARTADO


def test_discarded_item_cannot_be_reinjected() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
        records=(_record(1, valid=False),), context=context,
    )
    item = next(iter(repository.items.values()))
    service.discard(item_id=item.id, justification="no aplica", context=context)
    with pytest.raises(QuarantineError):
        service.reinject(item_id=item.id, corrected_payload=item.original_payload or {}, context=context)


def test_audit_legal_matter_only_position_is_reported_as_conforming() -> None:
    repository = InMemoryValidationRepository()
    repository.family = SourceFamily.INTERNAL_AUDIT
    service = _service(repository)
    result = service.validate(
        file_id=uuid4(),
        family=SourceFamily.INTERNAL_AUDIT,
        headers=_AUDIT_LEGAL_MATTER_HEADERS,
        records=(_audit_legal_matter_record(1, valid=True),),
        context=_context(),
    )
    assert result.conforming_positions == (1,)
    assert result.quarantined == ()


def test_validation_service_uses_composite_contracts_for_internal_audit(monkeypatch) -> None:
    import app.validation.service as service_module

    repository = InMemoryValidationRepository()
    repository.family = SourceFamily.INTERNAL_AUDIT
    service = _service(repository)
    structural_calls: list[SourceFamily] = []
    record_calls: list[SourceFamily] = []
    original_structural = service_module.validate_structure_for_family
    original_record = service_module.validate_record_for_family

    def structural(headers, rows, family):
        structural_calls.append(family)
        return original_structural(headers, rows, family)

    def record(values, family):
        record_calls.append(family)
        return original_record(values, family)

    monkeypatch.setattr(service_module, "validate_structure_for_family", structural)
    monkeypatch.setattr(service_module, "validate_record_for_family", record)
    service.validate(
        file_id=uuid4(),
        family=SourceFamily.INTERNAL_AUDIT,
        headers=_AUDIT_LEGAL_MATTER_HEADERS,
        records=(_audit_legal_matter_record(1, valid=True),),
        context=_context(),
    )
    assert structural_calls == [SourceFamily.INTERNAL_AUDIT]
    assert record_calls == [SourceFamily.INTERNAL_AUDIT]


def test_audit_legal_matter_reinjection_can_become_reinjected() -> None:
    repository = InMemoryValidationRepository()
    repository.family = SourceFamily.INTERNAL_AUDIT
    service = _service(repository)
    context = _context()
    service.validate(
        file_id=uuid4(),
        family=SourceFamily.INTERNAL_AUDIT,
        headers=_AUDIT_LEGAL_MATTER_HEADERS,
        records=(_audit_legal_matter_record(1, valid=False),),
        context=context,
    )
    item = next(iter(repository.items.values()))
    updated = service.reinject(
        item_id=item.id,
        corrected_payload={
            "ID_Asunto": "A-1",
            "Tipo_Asunto": "Auditoría",
            "Estado": "Abierto",
            "Fecha": "2026-03-01",
        },
        context=context,
    )
    assert updated.state == QuarantineState.REINYECTADO


def test_audit_multiple_invalid_contracts_create_one_quarantine_item() -> None:
    repository = InMemoryValidationRepository()
    repository.family = SourceFamily.INTERNAL_AUDIT
    service = _service(repository)
    record = TabularRecord(
        position=1,
        source_record_id=uuid4(),
        values_by_name={
            "ID_Incidente": "I-1",
            "Fecha_Evento": "2026-03-01",
            "Area": "Cumplimiento",
            "Nivel_Severidad": "crítico",
            "ID_Asunto": "A-1",
            "Tipo_Asunto": "Otro",
            "Estado": "Abierto",
            "Fecha": "2026-03-01",
        },
        width=8,
    )
    result = service.validate(
        file_id=uuid4(),
        family=SourceFamily.INTERNAL_AUDIT,
        headers=tuple(record.values_by_name),
        records=(record,),
        context=_context(),
    )
    assert result.quarantined[0][1] == QuarantineCause.OUT_OF_CATALOG
    assert len(repository.items) == 1


def test_invalid_audit_row_uses_existing_unified_quarantine() -> None:
    repository = InMemoryValidationRepository()
    repository.family = SourceFamily.INTERNAL_AUDIT
    service = _service(repository)
    result = service.validate(
        file_id=uuid4(),
        family=SourceFamily.INTERNAL_AUDIT,
        headers=_AUDIT_LEGAL_MATTER_HEADERS,
        records=(_audit_legal_matter_record(1, valid=False),),
        context=_context(),
    )
    assert result.quarantined == ((1, QuarantineCause.OUT_OF_CATALOG),)
    assert result.validated_records == ()
    item = next(iter(repository.items.values()))
    assert item.cause == QuarantineCause.OUT_OF_CATALOG


def test_validation_result_contains_exact_validated_record_snapshot() -> None:
    repository = InMemoryValidationRepository()
    record = _record(1, valid=True)
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=_HEADERS, records=(record,), context=_context(),
    )
    snapshot = result.validated_records[0]
    assert snapshot.source_record_id == record.source_record_id
    assert snapshot.position == record.position
    assert snapshot.family == SourceFamily.CONTRACTS_DOCUMENTS
    assert dict(snapshot.values_by_name) == record.values_by_name


def test_validated_records_have_bijection_with_conforming_positions() -> None:
    repository = InMemoryValidationRepository()
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
        records=(_record(1, valid=True), _record(2, valid=False), _record(3, valid=True)), context=_context(),
    )
    assert tuple(record.position for record in result.validated_records) == result.conforming_positions
    assert len(result.validated_records) == len(result.conforming_positions)


def test_mutating_original_nested_values_does_not_change_snapshot() -> None:
    repository = InMemoryValidationRepository()
    record = _record(1, valid=True)
    record.values_by_name["nested"] = {"items": ["before"]}
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=_HEADERS, records=(record,), context=_context(),
    )
    record.values_by_name["nested"]["items"].append("after")
    snapshot = result.validated_records[0]
    assert snapshot.values_by_name["nested"]["items"] == ("before",)
    with pytest.raises(TypeError):
        snapshot.values_by_name["new"] = "value"


def test_snapshot_failure_never_leaves_position_conforming() -> None:
    repository = InMemoryValidationRepository()
    record = _record(1, valid=True)
    cycle: list[object] = []
    cycle.append(cycle)
    record.values_by_name["nested"] = cycle
    with pytest.raises(ValidationInvocationError):
        _service(repository).validate(
            file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
            headers=_HEADERS, records=(record,), context=_context(),
        )
    assert repository.items == {}
    assert repository.transitions == []
    assert repository.audit_events == []


def test_legacy_validation_result_equality_behavior_is_preserved() -> None:
    structural = StructuralValidation(True, None)
    legacy = ValidationResult("file", SourceFamily.CONTRACTS_DOCUMENTS, structural, (1,), ())
    current = ValidationResult(
        "file", SourceFamily.CONTRACTS_DOCUMENTS, structural, (1,), (),
        (ValidatedTabularRecord(uuid4(), SourceFamily.CONTRACTS_DOCUMENTS, 1, {}),),
    )
    assert legacy == current


def test_family_mismatch_fails_without_quarantining_records() -> None:
    repository = InMemoryValidationRepository()
    with pytest.raises(ValidationInvocationError):
        _service(repository).validate(
            file_id=uuid4(), family=SourceFamily.INTERNAL_AUDIT,
            headers=_AUDIT_LEGAL_MATTER_HEADERS,
            records=(_audit_legal_matter_record(1, valid=True),), context=_context(),
        )
    assert repository.items == {}


def test_duplicate_input_positions_fail_without_publishing_validation_outcome() -> None:
    repository = InMemoryValidationRepository()
    with pytest.raises(ValidationInvocationError):
        _service(repository).validate(
            file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
            records=(_record(1, valid=True), _record(1, valid=False)), context=_context(),
        )
    assert repository.items == {}


def test_validation_rejects_source_record_identity_not_matching_file_and_position() -> None:
    repository = InMemoryValidationRepository()
    repository.reject_bindings = True
    with pytest.raises(ValidationInvocationError):
        _service(repository).validate(
            file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
            headers=_HEADERS, records=(_record(1, valid=True),), context=_context(),
        )
    assert repository.items == {}


def test_legacy_reinject_return_contract_is_unchanged() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
                     records=(_record(1, valid=False),), context=context)
    item = next(iter(repository.items.values()))
    assert isinstance(service.reinject(item_id=item.id, corrected_payload=_record(1, valid=True).values_by_name, context=context), QuarantineItem)


def test_reinject_with_validated_record_returns_authoritative_snapshot() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
                     records=(_record(4, valid=False),), context=context)
    item = next(iter(repository.items.values()))
    result = service.reinject_with_validated_record(
        item_id=item.id, corrected_payload=_record(4, valid=True).values_by_name, context=context
    )
    assert result.quarantine_item.state == QuarantineState.REINYECTADO
    assert result.validated_record is not None
    assert result.validated_record.source_record_id == item.source_record_id
    assert result.validated_record.position == 4


def test_validated_snapshot_preserves_family() -> None:
    repository = InMemoryValidationRepository()
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=_HEADERS, records=(_record(1, valid=True),), context=_context(),
    )
    assert result.validated_records[0].family == SourceFamily.CONTRACTS_DOCUMENTS


def test_validated_snapshot_preserves_source_record_identity() -> None:
    repository = InMemoryValidationRepository()
    record = _record(1, valid=True)
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=_HEADERS, records=(record,), context=_context(),
    )
    assert result.validated_records[0].source_record_id == record.source_record_id


def test_validated_snapshot_preserves_position() -> None:
    repository = InMemoryValidationRepository()
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=_HEADERS, records=(_record(7, valid=True),), context=_context(),
    )
    assert result.validated_records[0].position == 7


def test_mutating_original_record_after_validation_does_not_change_snapshot() -> None:
    repository = InMemoryValidationRepository()
    record = _record(1, valid=True)
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=_HEADERS, records=(record,), context=_context(),
    )
    record.values_by_name["Estado_Revision"] = "Pendiente"
    assert result.validated_records[0].values_by_name["Estado_Revision"] == "No iniciado"


def test_validated_snapshot_values_are_immutable() -> None:
    repository = InMemoryValidationRepository()
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=_HEADERS, records=(_record(1, valid=True),), context=_context(),
    )
    with pytest.raises(TypeError):
        result.validated_records[0].values_by_name["ID_Contrato"] = "C-2"


def test_existing_conforming_positions_contract_is_preserved() -> None:
    repository = InMemoryValidationRepository()
    result = _service(repository).validate(
        file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
        records=(_record(1, valid=True), _record(2, valid=False)), context=_context(),
    )
    assert result.conforming_positions == (1,)


def test_reinjection_produces_validated_snapshot_for_reinjected_record() -> None:
    repository = InMemoryValidationRepository()
    service = _service(repository)
    context = _context()
    service.validate(file_id=uuid4(), family=SourceFamily.CONTRACTS_DOCUMENTS, headers=_HEADERS,
                     records=(_record(2, valid=False),), context=context)
    item = next(iter(repository.items.values()))
    result = service.reinject_with_validated_record(
        item_id=item.id, corrected_payload=_record(2, valid=True).values_by_name, context=context
    )
    assert result.validated_record is not None
    assert result.quarantine_item.state == QuarantineState.REINYECTADO
