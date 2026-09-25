"""Targeted PostgreSQL evidence for the durable KPI integration boundary."""
from __future__ import annotations

import os
from pathlib import Path
from types import MappingProxyType
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.analytics.repository import AnalyticsRepository
from app.analytics.service import KPIRecalculationService
from app.coordination.kpi_integration import KpiIntegrationError, KpiIntegrationService
from app.coordination.kpi_repository import KpiIntegrationRepository
from app.ingestion.models import SourceFamily
from app.security.models import AuthenticatedPrincipal
from app.security.repository import SecurityRepository
from app.validation.models import ValidatedTabularRecord
from app.validation.repository import ValidationRepository
from app.validation.service import TabularRecord, ValidationContext, ValidationService


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _url() -> str:
    value = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("DATA_TEST_DATABASE_URL no configurada")
    if "test" not in (make_url(value).database or "").lower():
        pytest.fail("La evidencia requiere una base PostgreSQL de pruebas")
    return make_url(value).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _conninfo(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def database():
    url = _url()
    parsed = make_url(url)
    os.environ.update(POSTGRES_HOST=parsed.host or "localhost", POSTGRES_PORT=str(parsed.port or 5432), POSTGRES_USER=parsed.username or "", POSTGRES_PASSWORD=parsed.password or "", POSTGRES_DB=parsed.database or "")
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE app.job_run, app.kpi_observation, app.analytic_run, app.record_application, app.contract_record, app.quarantine_transition, app.quarantine_item, app.source_record, app.ingest_file, app.stored_object, audit.event, app.access_session, app.user_role, app.user_account CASCADE"))
    try:
        yield engine, _conninfo(url)
    finally:
        engine.dispose()


class _Security:
    def __init__(self, repository) -> None:
        self.repository = repository
    def revalidate_functional_access(self, *_args) -> None:
        return None


class _FailingProjection:
    def project_in_transaction(self, *_args, **_kwargs):
        raise RuntimeError("controlled projection failure")


class _FailingAnalytics:
    def recalculate(self, *_args, **_kwargs):
        raise RuntimeError("controlled core failure")


def _seed(engine) -> tuple[UUID, UUID, AuthenticatedPrincipal]:
    object_id, file_id, source_id, account_id = uuid4(), uuid4(), uuid4(), uuid4()
    username = f"integration-{account_id.hex[:8]}"
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :username, 'Integration', 'test-only-hash')"), {"id": account_id, "username": username})
        connection.execute(sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'text/csv', 1, 'integration.csv')"), {"id": object_id, "locator": f"objects/{object_id}", "sha": bytes(32)})
        connection.execute(sa.text("""INSERT INTO app.ingest_file
            (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
             declared_extension, detected_format, format_classification, technical_result,
             declared_name, source_locator, source_revision, actor_identifier, content_sha256)
            VALUES (:id, :object, 'CONTRATOS_DOCUMENTOS', 'CSV', 'COMPLETADO', :operation, :correlation,
                    '.csv', 'CSV', 'SUPPORTED', 'ACCEPTED', 'integration.csv', :locator, 1, 'test', :sha)"""),
            {"id": file_id, "object": object_id, "operation": uuid4(), "correlation": uuid4(), "locator": f"test/{file_id}", "sha": bytes(32)})
        connection.execute(sa.text("INSERT INTO app.source_record (id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state) VALUES (:id, :file, 1, 'CSV', '{}'::jsonb, :sha, 'EXTRAIDO')"), {"id": source_id, "file": file_id, "sha": bytes(32)})
    return file_id, source_id, AuthenticatedPrincipal(account_id, uuid4(), username, 1, frozenset({"ANALISTA"}), frozenset({"ingest.execute", "quarantine.reinject"}))


def _values(source_id: UUID, valid: bool = True) -> dict[str, object]:
    return {"ID_Contrato": f"C-{source_id.hex[:8]}", "Fecha_Solicitud": "2026-01-01", "Fecha_Firma": "2026-01-15", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado" if valid else "incorrecto"}


def _services(conninfo: str):
    audit = SecurityRepository(conninfo)
    security = _Security(audit)
    return (
        ValidationService(ValidationRepository(conninfo, audit), security),
        KpiIntegrationService(conninfo=conninfo, security=security, repository=KpiIntegrationRepository(conninfo)),
        audit,
    )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_certified_record_persists_projection_job_run_and_observation(database) -> None:
    engine, conninfo = database
    file_id, source_id, actor = _seed(engine)
    validation, integration, _ = _services(conninfo)
    operation_id, correlation_id = uuid4(), uuid4()
    records = validation.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=tuple(_values(source_id)), records=(TabularRecord(1, source_id, _values(source_id), 5),), context=ValidationContext(operation_id, correlation_id, actor)).validated_records
    assert integration.project_records(records=records, operation_id=operation_id, correlation_id=correlation_id, actor=actor).state == "COMPLETED"
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.contract_record WHERE source_record_id=:id"), {"id": source_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.record_application WHERE source_record_id=:id"), {"id": source_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.job_run WHERE operation_id=:id"), {"id": operation_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.analytic_run")).scalar_one() >= 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.kpi_observation")).scalar_one() >= 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_projection_failure_rolls_back_reinjection_and_all_application_effects(database) -> None:
    engine, conninfo = database
    file_id, source_id, actor = _seed(engine)
    validation, integration, _ = _services(conninfo)
    validation.validate(file_id=file_id, family=SourceFamily.CONTRACTS_DOCUMENTS, headers=tuple(_values(source_id)), records=(TabularRecord(1, source_id, _values(source_id, False), 5),), context=ValidationContext(uuid4(), uuid4(), actor))
    with engine.connect() as connection:
        item_id = connection.execute(sa.text("SELECT id FROM app.quarantine_item WHERE source_record_id=:id"), {"id": source_id}).scalar_one()
    integration.projection = _FailingProjection()
    with pytest.raises(RuntimeError, match="controlled projection failure"):
        integration.reinject(item_id=item_id, corrected_payload=_values(source_id), correlation_id=uuid4(), actor=actor, validation=validation)
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT state FROM app.quarantine_item WHERE id=:id"), {"id": item_id}).scalar_one() == "Pendiente"
        assert connection.execute(sa.text("SELECT count(*) FROM app.contract_record WHERE source_record_id=:id"), {"id": source_id}).scalar_one() == 0
        assert connection.execute(sa.text("SELECT count(*) FROM app.record_application WHERE source_record_id=:id"), {"id": source_id}).scalar_one() == 0
        assert connection.execute(sa.text("SELECT count(*) FROM app.job_run")).scalar_one() == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_failed_core_job_retries_without_duplicate_projection(database) -> None:
    engine, conninfo = database
    _, source_id, actor = _seed(engine)
    _, integration, audit = _services(conninfo)
    operation_id, correlation_id = uuid4(), uuid4()
    record = ValidatedTabularRecord(source_id, SourceFamily.CONTRACTS_DOCUMENTS, 1, MappingProxyType(_values(source_id)))
    integration.analytics = _FailingAnalytics()
    with pytest.raises(KpiIntegrationError):
        integration.project_records(records=(record,), operation_id=operation_id, correlation_id=correlation_id, actor=actor)
    with engine.connect() as connection:
        job = connection.execute(sa.text("SELECT id, state FROM app.job_run WHERE operation_id=:id"), {"id": operation_id}).mappings().one()
        assert job["state"] == "FAILED"
    integration.analytics = KPIRecalculationService(AnalyticsRepository(conninfo), audit)
    retried = integration.project_records(records=(record,), operation_id=operation_id, correlation_id=uuid4(), actor=actor)
    assert retried.job_id == job["id"] and retried.state == "COMPLETED"
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.record_application WHERE source_record_id=:id"), {"id": source_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.job_run WHERE operation_id=:id"), {"id": operation_id}).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.kpi_observation")).scalar_one() >= 1
