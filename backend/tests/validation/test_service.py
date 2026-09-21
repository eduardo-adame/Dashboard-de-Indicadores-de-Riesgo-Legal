"""Pruebas del servicio de validación y cuarentena con repositorio en memoria."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest

from app.ingestion.models import SourceFamily
from app.security.models import AuthenticatedPrincipal
from app.validation.models import QuarantineCause, QuarantineState
from app.validation.quarantine import QuarantineError, QuarantineItem
from app.validation.service import TabularRecord, ValidationContext, ValidationService


class InMemoryValidationRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, QuarantineItem] = {}
        self.transitions: list[object] = []
        self.audit_events: list[dict] = []

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
        return SourceFamily.CONTRACTS_DOCUMENTS

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


def _record(position: int, *, valid: bool) -> TabularRecord:
    values = {
        "ID_Contrato": f"C-{position}",
        "Fecha_Solicitud": "2026-01-10",
        "Fecha_Firma": "2026-01-15",
        "Fecha_Vencimiento": "2026-06-30",
        "Estado_Revision": "No iniciado" if valid else "Pendiente",
    }
    return TabularRecord(position=position, source_record_id=uuid4(), values_by_name=values, width=5)


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
