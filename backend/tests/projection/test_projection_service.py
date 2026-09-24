from __future__ import annotations

from contextlib import contextmanager
from types import MappingProxyType
from uuid import UUID, uuid4

import pytest

from app.ingestion.models import SourceFamily
from app.projection.models import ProjectionContext, ProjectionError, ProjectionInvariantError, SourceRecordProvenance
from app.projection.service import ProjectionService
from app.security.models import AuthenticatedPrincipal
from app.validation.models import ValidatedTabularRecord


class _Repository:
    def __init__(self) -> None:
        self.provenance: dict[UUID, SourceRecordProvenance] = {}
        self.entities: dict[tuple[str, str], dict[str, object]] = {}
        self.applications: dict[tuple[UUID, str, str], dict[str, object]] = {}
        self.quarantines: list[dict[str, object]] = []

    @contextmanager
    def transaction(self):
        yield self

    def source_record_provenance(self, _connection, source_record_id: UUID):
        return self.provenance.get(source_record_id)

    def application_for_target(self, _connection, *, source_record_id: UUID, entity_type: str, business_id: str):
        return self.applications.get((source_record_id, entity_type, business_id))

    def entity_row(self, _connection, *, entity_type: str, business_id: str):
        return self.entities.get((entity_type, business_id))

    def related_exists(self, _connection, *, table: str, identifier: str) -> bool:
        entity_type = {"contract_record": "CONTRATO", "compliance_obligation": "OBLIGACION"}[table]
        return (entity_type, identifier) in self.entities

    def insert_entity(self, _connection, *, table: str, values: dict[str, object]) -> bool:
        entity_type, identifier_column = {
            "contract_record": ("CONTRATO", "id_contrato"), "litigation": ("LITIGIO", "id_litigio"),
            "compliance_obligation": ("OBLIGACION", "id_obligacion"), "incident": ("INCIDENTE", "id_incidente"),
            "legal_matter": ("ASUNTO", "id_asunto"),
        }[table]
        key = (entity_type, str(values[identifier_column]))
        if key in self.entities:
            return False
        self.entities[key] = dict(values)
        return True

    def insert_application(self, _connection, **kwargs) -> bool:
        key = (kwargs["source_record_id"], kwargs["entity_type"], kwargs["business_id"])
        if key in self.applications:
            return False
        self.applications[key] = {"result": kwargs["result"], "payload_sha256": kwargs["payload_sha256"], "operation_id": kwargs["operation_id"]}
        return True

    def insert_conflict_quarantine(self, _connection, **kwargs) -> bool:
        if any(item["operation_id"] == kwargs["operation_id"] for item in self.quarantines):
            return False
        self.quarantines.append(dict(kwargs))
        return True


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), "analyst", 1, frozenset({"ANALISTA"}), frozenset({"ingest.execute"}))


def _context() -> ProjectionContext:
    return ProjectionContext(uuid4(), uuid4(), _principal())


def _record(source_record_id: UUID, *, contract_id: str = "C-1", request_date: str = "2026-01-01") -> ValidatedTabularRecord:
    return ValidatedTabularRecord(source_record_id, SourceFamily.CONTRACTS_DOCUMENTS, 1, MappingProxyType({
        "ID_Contrato": contract_id, "Fecha_Solicitud": request_date, "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado",
    }))


def _service_with(*record_ids: UUID) -> tuple[_Repository, ProjectionService]:
    repository = _Repository()
    for record_id in record_ids:
        repository.provenance[record_id] = SourceRecordProvenance(record_id, uuid4())
    return repository, ProjectionService(repository)


def test_incorporated_result_requires_recalculation() -> None:
    source = uuid4()
    _, service = _service_with(source)
    result = service.project(_record(source), _context())
    assert result[0].result == "INCORPORADO"
    assert result[0].recalculation_required is True


def test_retry_of_historical_incorporated_application_returns_idempotent_without_recalculation() -> None:
    source = uuid4()
    repository, service = _service_with(source)
    context = _context()
    service.project(_record(source), context)
    retry = service.project(_record(source), context)
    assert repository.applications[(source, "CONTRATO", "C-1")]["result"] == "INCORPORADO"
    assert retry[0].result == "IDEMPOTENTE"
    assert retry[0].recalculation_required is False


def test_retry_does_not_mutate_historical_record_application_result() -> None:
    source = uuid4()
    repository, service = _service_with(source)
    context = _context()
    service.project(_record(source), context)
    before = dict(repository.applications[(source, "CONTRATO", "C-1")])
    service.project(_record(source), context)
    assert repository.applications[(source, "CONTRATO", "C-1")] == before


def test_idempotent_result_does_not_require_recalculation() -> None:
    first, second = uuid4(), uuid4()
    _, service = _service_with(first, second)
    context = _context()
    service.project(_record(first), context)
    result = service.project(_record(second), context)
    assert result[0].result == "IDEMPOTENTE"
    assert result[0].recalculation_required is False


def test_retry_of_historical_idempotent_application_returns_idempotent_without_recalculation() -> None:
    first, second = uuid4(), uuid4()
    repository, service = _service_with(first, second)
    context = _context()
    service.project(_record(first), context)
    service.project(_record(second), context)
    retry = service.project(_record(second), context)
    assert repository.applications[(second, "CONTRATO", "C-1")]["result"] == "IDEMPOTENTE"
    assert retry[0].result == "IDEMPOTENTE"
    assert retry[0].recalculation_required is False


def test_different_source_same_business_different_payload_is_controlled_conflict() -> None:
    first, second = uuid4(), uuid4()
    repository, service = _service_with(first, second)
    context = _context()
    service.project(_record(first), context)
    result = service.project(_record(second, request_date="2026-01-02"), context)
    assert result[0].result == "CONFLICTO"
    assert result[0].recalculation_required is False
    assert repository.applications[(second, "CONTRATO", "C-1")]["result"] == "CONFLICTO"
    assert len(repository.quarantines) == 1


def test_retry_of_historical_conflict_returns_conflict_without_new_effect() -> None:
    first, second = uuid4(), uuid4()
    repository, service = _service_with(first, second)
    context = _context()
    service.project(_record(first), context)
    candidate = _record(second, request_date="2026-01-02")
    service.project(candidate, context)
    entity_before = dict(repository.entities[("CONTRATO", "C-1")])
    retry = service.project(candidate, context)
    assert retry[0].result == "CONFLICTO"
    assert retry[0].recalculation_required is False
    assert repository.entities[("CONTRATO", "C-1")] == entity_before
    assert len(repository.applications) == 2


def test_retry_of_historical_conflict_does_not_duplicate_quarantine() -> None:
    first, second = uuid4(), uuid4()
    repository, service = _service_with(first, second)
    context = _context()
    service.project(_record(first), context)
    candidate = _record(second, request_date="2026-01-02")
    service.project(candidate, context)
    service.project(candidate, context)
    assert len(repository.quarantines) == 1


def test_retry_of_historical_conflict_does_not_request_recalculation() -> None:
    first, second = uuid4(), uuid4()
    _, service = _service_with(first, second)
    context = _context()
    service.project(_record(first), context)
    candidate = _record(second, request_date="2026-01-02")
    service.project(candidate, context)
    assert service.project(candidate, context)[0].recalculation_required is False


def test_existing_application_different_hash_is_invariant_error_without_new_quarantine() -> None:
    source = uuid4()
    repository, service = _service_with(source)
    context = _context()
    service.project(_record(source), context)
    with pytest.raises(ProjectionInvariantError):
        service.project(_record(source, request_date="2026-01-02"), context)
    assert len(repository.quarantines) == 0
    assert repository.applications[(source, "CONTRATO", "C-1")]["result"] == "INCORPORADO"


def test_missing_source_record_fails_without_persistence() -> None:
    repository, service = _service_with()
    with pytest.raises(ProjectionError):
        service.project(_record(uuid4()), _context())
    assert repository.entities == {}
    assert repository.applications == {}


def test_multi_target_snapshot_returns_one_result_per_target_with_distinct_child_operations() -> None:
    source = uuid4()
    _, service = _service_with(source)
    snapshot = ValidatedTabularRecord(source, SourceFamily.COMPLIANCE, 1, MappingProxyType({
        "ID_Obligacion": "O-1", "Fecha_Limite": "2026-01-01",
        "ID_Incidente": "I-1", "Fecha_Evento": "2026-01-02", "Area": "Legal", "Nivel_Severidad": "alto",
        "ID_Asunto": "A-1", "Tipo_Asunto": "Cumplimiento", "Estado": "Abierto", "Fecha": "2026-01-03",
    }))
    results = service.project(snapshot, _context())
    assert [result.entity_type for result in results] == ["OBLIGACION", "INCIDENTE", "ASUNTO"]
    assert len({result.operation_id for result in results}) == 3
