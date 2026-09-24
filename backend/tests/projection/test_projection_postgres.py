from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import os
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.ingestion.models import SourceFamily
from app.projection.models import ProjectionContext
from app.projection.repository import ProjectionRepository
from app.projection.service import ProjectionService
from app.security.models import AuthenticatedPrincipal
from app.validation.repository import ValidationRepository
from app.validation.service import TabularRecord, ValidationContext, ValidationService
from app.validation.models import ValidatedTabularRecord


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _url() -> str:
    value = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("DATA_TEST_DATABASE_URL no configurada")
    if "test" not in (make_url(value).database or "").lower():
        pytest.fail("La evidencia requiere una base desechable")
    return make_url(value).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _conninfo(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def database():
    url = _url()
    parsed = make_url(url)
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost", POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "", POSTGRES_PASSWORD=parsed.password or "", POSTGRES_DB=parsed.database or "",
    )
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    engine = sa.create_engine(url)
    try:
        yield engine, _conninfo(url)
    finally:
        engine.dispose()


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(uuid4(), uuid4(), "analyst", 1, frozenset({"ANALISTA"}), frozenset({"ingest.execute"}))


def _projection_context() -> ProjectionContext:
    return ProjectionContext(uuid4(), uuid4(), _principal())


def _seed_source(engine: sa.Engine, family: SourceFamily, position: int = 1) -> tuple[UUID, UUID]:
    file_id, object_id, source_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text("""INSERT INTO app.stored_object
            (id, storage_kind, locator, sha256, mime_type, byte_size, original_name)
            VALUES (:id, 'FILESYSTEM', :locator, :sha, 'text/csv', 1, 'projection.csv')"""),
            {"id": object_id, "locator": f"objects/{object_id}", "sha": bytes(32)})
        connection.execute(sa.text("""INSERT INTO app.ingest_file
            (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
             declared_extension, detected_format, format_classification, technical_result,
             declared_name, source_locator, source_revision, actor_identifier, content_sha256)
            VALUES (:id, :object_id, :family, 'CSV', 'COMPLETADO', :operation_id, :correlation_id,
                    '.csv', 'CSV', 'SUPPORTED', 'ACCEPTED', 'projection.csv', :locator, 1, 'test', :sha)"""),
            {"id": file_id, "object_id": object_id, "family": family.value, "operation_id": uuid4(),
             "correlation_id": uuid4(), "locator": f"test/{file_id}", "sha": bytes(32)})
        connection.execute(sa.text("""INSERT INTO app.source_record
            (id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state)
            VALUES (:id, :file_id, :position, 'CSV', '{}'::jsonb, :sha, 'EXTRAIDO')"""),
            {"id": source_id, "file_id": file_id, "position": position, "sha": bytes(32)})
    return file_id, source_id


def _snapshot(source_id: UUID, family: SourceFamily, values: dict[str, object]) -> ValidatedTabularRecord:
    return ValidatedTabularRecord(source_id, family, 1, MappingProxyType(values))


def _contract(source_id: UUID, *, contract_id: str, request_date: str = "2026-01-01") -> ValidatedTabularRecord:
    return _snapshot(source_id, SourceFamily.CONTRACTS_DOCUMENTS, {
        "ID_Contrato": contract_id, "Fecha_Solicitud": request_date,
        "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado",
    })


class _Security:
    def revalidate_functional_access(self, *_args) -> None:
        return None


class _AuditRepository:
    def write_audit_event(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize(("family", "values", "table"), [
    (SourceFamily.CONTRACTS_DOCUMENTS, {"ID_Contrato": "C", "Fecha_Solicitud": "2026-01-01", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado"}, "contract_record"),
    (SourceFamily.LITIGATION, {"ID_Litigio": "L", "Fecha_Apertura": "2026-01-01", "Estado": "Activo", "Nivel_Severidad": "alto"}, "litigation"),
    (SourceFamily.COMPLIANCE, {"ID_Obligacion": "O", "Fecha_Limite": "2026-01-01"}, "compliance_obligation"),
    (SourceFamily.INTERNAL_AUDIT, {"ID_Incidente": "I", "Fecha_Evento": "2026-01-01", "Area": "Legal", "Nivel_Severidad": "alto"}, "incident"),
    (SourceFamily.INTERNAL_AUDIT, {"ID_Asunto": "A", "Tipo_Asunto": "Auditoría", "Estado": "Abierto", "Fecha": "2026-01-01"}, "legal_matter"),
])
def test_validated_snapshot_persists_analytical_entities(database, family: SourceFamily, values: dict[str, object], table: str) -> None:
    engine, conninfo = database
    _, source_id = _seed_source(engine, family)
    unique_values = {key: (f"{value}-{source_id.hex[:8]}" if key.startswith("ID_") else value) for key, value in values.items()}
    result = ProjectionService(ProjectionRepository(conninfo)).project(_snapshot(source_id, family, unique_values), _projection_context())
    assert result[0].result == "INCORPORADO"
    with engine.connect() as connection:
        assert connection.execute(sa.text(f"SELECT count(*) FROM app.{table} WHERE source_record_id = :source"), {"source": source_id}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_audit_incident_and_legal_matter_keep_legal_matter_provenance_independent_in_database(database) -> None:
    engine, conninfo = database
    _, source_id = _seed_source(engine, SourceFamily.INTERNAL_AUDIT)
    service = ProjectionService(ProjectionRepository(conninfo))
    results = service.project(_snapshot(source_id, SourceFamily.INTERNAL_AUDIT, {
        "ID_Incidente": f"I-{source_id.hex[:8]}", "Fecha_Evento": "2026-01-01", "Area": "Legal", "Nivel_Severidad": "alto",
        "ID_Asunto": f"A-{source_id.hex[:8]}", "Tipo_Asunto": "Auditoría", "Estado": "Abierto", "Fecha": "2026-01-01",
    }), _projection_context())
    assert [result.entity_type for result in results] == ["INCIDENTE", "ASUNTO"]
    with engine.connect() as connection:
        row = connection.execute(sa.text("SELECT source_entity_type, source_business_id FROM app.legal_matter WHERE source_record_id = :source"), {"source": source_id}).mappings().one()
    assert dict(row) == {"source_entity_type": "AUDITORIA_INTERNA", "source_business_id": f"A-{source_id.hex[:8]}"}


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_validation_emits_the_snapshot_consumed_by_projection(database) -> None:
    engine, conninfo = database
    file_id, source_id = _seed_source(engine, SourceFamily.CONTRACTS_DOCUMENTS)
    validation = ValidationService(ValidationRepository(conninfo, _AuditRepository()), _Security())
    context = ValidationContext(uuid4(), uuid4(), _principal())
    validation_result = validation.validate(
        file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=("ID_Contrato", "Fecha_Solicitud", "Fecha_Vencimiento", "Estado_Revision"),
        records=(TabularRecord(1, source_id, {"ID_Contrato": f"C-{source_id.hex[:8]}", "Fecha_Solicitud": "2026-01-01", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado"}, 4),),
        context=context,
    )
    snapshot = validation_result.validated_records[0]
    result = ProjectionService(ProjectionRepository(conninfo)).project(snapshot, _projection_context())
    assert result[0].source_record_id == snapshot.source_record_id


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_reinjected_snapshot_is_consumed_by_projection(database) -> None:
    engine, conninfo = database
    file_id, source_id = _seed_source(engine, SourceFamily.CONTRACTS_DOCUMENTS)
    validation = ValidationService(ValidationRepository(conninfo, _AuditRepository()), _Security())
    context = ValidationContext(uuid4(), uuid4(), _principal())
    validation.validate(
        file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=("ID_Contrato", "Fecha_Solicitud", "Fecha_Vencimiento", "Estado_Revision"),
        records=(TabularRecord(1, source_id, {"ID_Contrato": f"C-{source_id.hex[:8]}", "Fecha_Solicitud": "2026-01-01", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "incorrecto"}, 4),),
        context=context,
    )
    with engine.connect() as connection:
        item_id = connection.execute(sa.text("SELECT id FROM app.quarantine_item WHERE source_record_id = :source"), {"source": source_id}).scalar_one()
    reinjected = validation.reinject_with_validated_record(
        item_id=item_id, corrected_payload={"ID_Contrato": f"C-{source_id.hex[:8]}", "Fecha_Solicitud": "2026-01-01", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado"}, context=context,
    )
    assert reinjected.validated_record is not None
    result = ProjectionService(ProjectionRepository(conninfo)).project(reinjected.validated_record, _projection_context())
    assert result[0].result == "INCORPORADO"


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_out_of_order_litigation_keeps_optional_contract_null(database) -> None:
    engine, conninfo = database
    _, source_id = _seed_source(engine, SourceFamily.LITIGATION)
    ProjectionService(ProjectionRepository(conninfo)).project(_snapshot(source_id, SourceFamily.LITIGATION, {
        "ID_Litigio": f"L-{source_id.hex[:8]}", "Fecha_Apertura": "2026-01-01", "Estado": "Activo", "Nivel_Severidad": "alto", "ID_Contrato": "not-loaded",
    }), _projection_context())
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT id_contrato FROM app.litigation WHERE source_record_id = :source"), {"source": source_id}).scalar_one() is None


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_equivalent_sources_are_idempotent_without_duplicate_entities(database) -> None:
    engine, conninfo = database
    _, first = _seed_source(engine, SourceFamily.CONTRACTS_DOCUMENTS)
    _, second = _seed_source(engine, SourceFamily.CONTRACTS_DOCUMENTS)
    service = ProjectionService(ProjectionRepository(conninfo))
    business_id = f"C-{uuid4().hex[:8]}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result()[0].result for future in [
            executor.submit(service.project, _contract(first, contract_id=business_id), _projection_context()),
            executor.submit(service.project, _contract(second, contract_id=business_id), _projection_context()),
        ]]
    assert set(results) == {"INCORPORADO", "IDEMPOTENTE"}
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.contract_record WHERE id_contrato = :id"), {"id": business_id}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_different_sources_create_one_entity_and_one_controlled_conflict(database) -> None:
    engine, conninfo = database
    _, first = _seed_source(engine, SourceFamily.CONTRACTS_DOCUMENTS)
    _, second = _seed_source(engine, SourceFamily.CONTRACTS_DOCUMENTS)
    service = ProjectionService(ProjectionRepository(conninfo))
    business_id = f"C-{uuid4().hex[:8]}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result()[0].result for future in [
            executor.submit(service.project, _contract(first, contract_id=business_id, request_date="2026-01-01"), _projection_context()),
            executor.submit(service.project, _contract(second, contract_id=business_id, request_date="2026-01-02"), _projection_context()),
        ]]
    assert set(results) == {"INCORPORADO", "CONFLICTO"}
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.contract_record WHERE id_contrato = :id"), {"id": business_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.record_application WHERE entity_type = 'CONTRATO' AND business_id = :id AND result = 'CONFLICTO'"), {"id": business_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.quarantine_item WHERE entity_type = 'CONTRATO' AND business_id = :id AND cause_code = 'IDENTITY_CONFLICT'"), {"id": business_id}).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_concurrent_retry_of_same_source_does_not_duplicate_application_or_effect(database) -> None:
    engine, conninfo = database
    _, source_id = _seed_source(engine, SourceFamily.CONTRACTS_DOCUMENTS)
    service = ProjectionService(ProjectionRepository(conninfo))
    context = _projection_context()
    snapshot = _contract(source_id, contract_id=f"C-{uuid4().hex[:8]}")
    service.project(snapshot, context)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result()[0] for future in [
            executor.submit(service.project, snapshot, context),
            executor.submit(service.project, snapshot, context),
        ]]
    assert {result.result for result in results} == {"IDEMPOTENTE"}
    assert all(result.recalculation_required is False for result in results)
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.record_application WHERE source_record_id = :source AND entity_type = 'CONTRATO'"), {"source": source_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.quarantine_item WHERE source_record_id = :source"), {"source": source_id}).scalar_one() == 0
