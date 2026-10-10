"""Targeted PostgreSQL evidence for the durable KPI integration boundary."""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
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


def _seed(engine, *, family: SourceFamily = SourceFamily.CONTRACTS_DOCUMENTS) -> tuple[UUID, UUID, AuthenticatedPrincipal]:
    object_id, file_id, source_id, account_id = uuid4(), uuid4(), uuid4(), uuid4()
    username = f"integration-{account_id.hex[:8]}"
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :username, 'Integration', 'test-only-hash')"), {"id": account_id, "username": username})
        connection.execute(sa.text("INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) VALUES (:id, 'FILESYSTEM', :locator, :sha, 'text/csv', 1, 'integration.csv')"), {"id": object_id, "locator": f"objects/{object_id}", "sha": bytes(32)})
        connection.execute(sa.text("""INSERT INTO app.ingest_file
            (id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id,
             declared_extension, detected_format, format_classification, technical_result,
             declared_name, source_locator, source_revision, actor_identifier, content_sha256)
            VALUES (:id, :object, :family, 'CSV', 'COMPLETADO', :operation, :correlation,
                    '.csv', 'CSV', 'SUPPORTED', 'ACCEPTED', 'integration.csv', :locator, 1, 'test', :sha)"""),
            {"id": file_id, "object": object_id, "family": family.value, "operation": uuid4(), "correlation": uuid4(), "locator": f"test/{file_id}", "sha": bytes(32)})
        connection.execute(sa.text("INSERT INTO app.source_record (id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state) VALUES (:id, :file, 1, 'CSV', '{}'::jsonb, :sha, 'EXTRAIDO')"), {"id": source_id, "file": file_id, "sha": bytes(32)})
    return file_id, source_id, AuthenticatedPrincipal(account_id, uuid4(), username, 1, frozenset({"ANALISTA"}), frozenset({"ingest.execute", "quarantine.reinject"}))


def _values(source_id: UUID, valid: bool = True) -> dict[str, object]:
    return {"ID_Contrato": f"C-{source_id.hex[:8]}", "Fecha_Solicitud": "2026-01-01", "Fecha_Firma": "2026-01-15", "Fecha_Vencimiento": "2026-12-31", "Estado_Revision": "No iniciado" if valid else "incorrecto"}


def _additional_source(engine, file_id: UUID, row_number: int) -> UUID:
    source_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO app.source_record "
                "(id, ingest_file_id, row_number, source_sheet, raw_payload, record_sha256, extraction_state) "
                "VALUES (:id, :file, :row, 'CSV', '{}'::jsonb, :sha, 'EXTRAIDO')"
            ),
            {"id": source_id, "file": file_id, "row": row_number, "sha": bytes([row_number]) * 32},
        )
    return source_id


def _preserve_proactive_snapshot(engine, observation: dict[str, object]) -> None:
    job_id, run_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """INSERT INTO app.job_run
                   (id, case_type, state, actor_process, result_references, operation_id, correlation_id)
                   VALUES (:id, 'PROACTIVE_ANALYSIS', 'STARTED', 'integration-test', '{}'::jsonb,
                           :operation, :correlation)"""
            ),
            {"id": job_id, "operation": uuid4(), "correlation": uuid4()},
        )
        connection.execute(
            sa.text(
                """INSERT INTO app.analytic_run
                   (id, run_type, state, window_start, window_end, kpi_codes, dimensions,
                    rules_reference, result, operation_id, correlation_id, completed_at)
                   VALUES (:id, 'PROACTIVE_ANALYSIS', 'COMPLETED', :start, :end,
                           ARRAY[:kpi_code], '{}'::jsonb, '{}'::jsonb, 'SUCCESS',
                           :operation, :correlation, CURRENT_TIMESTAMP)"""
            ),
            {
                "id": run_id,
                "start": observation["period_start"],
                "end": observation["period_end"],
                "kpi_code": observation["kpi_code"],
                "operation": uuid4(),
                "correlation": uuid4(),
            },
        )
        connection.execute(
            sa.text(
                """INSERT INTO app.proactive_input_snapshot
                   (id, analytic_run_id, job_run_id, source_observation_id,
                    source_analytic_run_id, kpi_code, canonical_dimensions_key,
                    period_start, period_end, as_of_date, availability, value, calculated_at)
                   VALUES (:id, :run, :job, :observation, :source_run, :kpi_code,
                           '{}'::jsonb, :start, :end, :as_of, :availability, :value, :calculated_at)"""
            ),
            {
                "id": uuid4(),
                "run": run_id,
                "job": job_id,
                "observation": observation["id"],
                "source_run": observation["analytic_run_id"],
                "kpi_code": observation["kpi_code"],
                "start": observation["period_start"],
                "end": observation["period_end"],
                "as_of": observation["as_of_date"],
                "availability": observation["availability"],
                "value": observation["value"],
                "calculated_at": observation["calculated_at"],
            },
        )


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


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_reinjection_recalculates_rc03_when_current_observation_has_historical_snapshot(database) -> None:
    engine, conninfo = database
    file_id, first_source, actor = _seed(engine)
    second_source = _additional_source(engine, file_id, 2)
    quarantined_source = _additional_source(engine, file_id, 3)
    validation, integration, _ = _services(conninfo)
    reference = datetime.now(UTC).date()
    due_date = (reference + timedelta(days=30)).isoformat()

    def values(source_id: UUID, *, valid: bool = True) -> dict[str, object]:
        payload = _values(source_id, valid)
        payload["Fecha_Vencimiento"] = due_date
        return payload

    initial_records = tuple(
        ValidatedTabularRecord(
            source_id,
            SourceFamily.CONTRACTS_DOCUMENTS,
            row_number,
            MappingProxyType(values(source_id)),
        )
        for row_number, source_id in enumerate((first_source, second_source), start=1)
    )
    initial = integration.project_records(
        records=initial_records,
        operation_id=uuid4(),
        correlation_id=uuid4(),
        actor=actor,
    )
    assert initial.state == "COMPLETED"

    validation.validate(
        file_id=file_id,
        family=SourceFamily.CONTRACTS_DOCUMENTS,
        headers=tuple(values(quarantined_source)),
        records=(TabularRecord(3, quarantined_source, values(quarantined_source, valid=False), 5),),
        context=ValidationContext(uuid4(), uuid4(), actor),
    )
    with engine.connect() as connection:
        item_id = connection.execute(
            sa.text("SELECT id FROM app.quarantine_item WHERE source_record_id=:id"),
            {"id": quarantined_source},
        ).scalar_one()
        before = connection.execute(
            sa.text("SELECT * FROM app.kpi_observation WHERE kpi_code='KPI-RC-03'"),
        ).mappings().one()
    assert before["value"] == 2
    _preserve_proactive_snapshot(engine, dict(before))

    correlation_id = uuid4()
    first = integration.reinject(
        item_id=item_id,
        corrected_payload=values(quarantined_source),
        correlation_id=correlation_id,
        actor=actor,
        validation=validation,
    )
    second = integration.reinject(
        item_id=item_id,
        corrected_payload=values(quarantined_source),
        correlation_id=uuid4(),
        actor=actor,
        validation=validation,
    )

    assert first.state == second.state == "COMPLETED"
    assert first.job_id == second.job_id
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT value FROM app.kpi_observation WHERE kpi_code='KPI-RC-03'"),
        ).scalar_one() == 3
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.contract_record WHERE id_contrato=:id"),
            {"id": values(quarantined_source)["ID_Contrato"]},
        ).scalar_one() == 1
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.kpi_observation WHERE kpi_code='KPI-RC-03'"),
        ).scalar_one() == 1
        audit = connection.execute(
            sa.text(
                "SELECT action, result FROM audit.event "
                "WHERE correlation_id=:correlation ORDER BY occurred_at"
            ),
            {"correlation": correlation_id},
        ).mappings().all()
    assert {row["action"] for row in audit} >= {"QUARANTINE_REINJECT", "KPI_RECALCULATION"}
    assert any(
        row["action"] == "QUARANTINE_REINJECT" and row["result"] == "REINYECTADO"
        for row in audit
    )
    assert all(
        row["result"] == "SUCCESS"
        for row in audit
        if row["action"] == "KPI_RECALCULATION"
    )
    assert not any(row["result"] == "FAILURE" for row in audit)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_late_litigation_reinjection_refreshes_monthly_count_and_preserves_snapshot(database) -> None:
    """SRS_REQUIRED: recálculo tardío, identidad única y evidencia histórica intacta."""
    engine, conninfo = database
    file_id, source_id, actor = _seed(engine, family=SourceFamily.LITIGATION)
    validation, integration, _ = _services(conninfo)
    business_id = f"L-{source_id.hex[:8]}"
    payload = {
        "ID_Litigio": business_id,
        "Fecha_Apertura": "2026-04-05",
        "Estado": "Activo",
        "Nivel_Severidad": "medio",
        "Monto_Reclamado": "10",
        "Estimacion_Interna": "",
    }
    invalid = dict(payload, Nivel_Severidad="incorrecto")
    validation.validate(
        file_id=file_id,
        family=SourceFamily.LITIGATION,
        headers=tuple(payload),
        records=(TabularRecord(1, source_id, invalid, len(payload)),),
        context=ValidationContext(uuid4(), uuid4(), actor),
    )
    integration.schedule(operation_id=uuid4(), correlation_id=uuid4(), reference=date(2026, 4, 10))
    with engine.connect() as connection:
        item_id = connection.execute(
            sa.text("SELECT id FROM app.quarantine_item WHERE source_record_id=:id"),
            {"id": source_id},
        ).scalar_one()
        before = dict(connection.execute(sa.text(
            "SELECT * FROM app.kpi_observation WHERE kpi_code='KPI-LI-05' "
            "AND period_start='2026-04-01'"
        )).mappings().one())
    assert before["value"] == 0
    assert before["as_of_date"] == date(2026, 4, 10)
    _preserve_proactive_snapshot(engine, before)
    with engine.connect() as connection:
        historical = connection.execute(sa.text(
            "SELECT to_jsonb(snapshot)::text FROM app.proactive_input_snapshot snapshot ORDER BY id"
        )).scalars().all()
    correlation_id = uuid4()
    first = integration.reinject(
        item_id=item_id, corrected_payload=payload, correlation_id=correlation_id,
        actor=actor, validation=validation,
    )
    with engine.connect() as connection:
        after = dict(connection.execute(sa.text(
            "SELECT * FROM app.kpi_observation WHERE id=:id"
        ), {"id": before["id"]}).mappings().one())
        runs_after = connection.execute(sa.text("SELECT count(*) FROM app.analytic_run")).scalar_one()
        events_after = connection.execute(sa.text("SELECT count(*) FROM audit.event")).scalar_one()
        application_date = connection.execute(sa.text(
            "SELECT (applied_at AT TIME ZONE 'UTC')::date FROM app.record_application "
            "WHERE source_record_id=:id AND entity_type='LITIGIO'"
        ), {"id": source_id}).scalar_one()
    assert after["value"] == 1
    assert after["id"] == before["id"]
    assert after["analytic_run_id"] == before["analytic_run_id"]
    assert after["as_of_date"] == max(date(2026, 4, 1), min(application_date, date(2026, 4, 30)))
    second = integration.reinject(
        item_id=item_id, corrected_payload=payload, correlation_id=uuid4(),
        actor=actor, validation=validation,
    )
    assert first.state == second.state == "COMPLETED"
    assert first.quarantine_state == second.quarantine_state == "Reinyectado"
    assert first.job_id == second.job_id and first.operation_id == second.operation_id
    with engine.connect() as connection:
        assert dict(connection.execute(sa.text(
            "SELECT * FROM app.kpi_observation WHERE id=:id"
        ), {"id": before["id"]}).mappings().one()) == after
        assert connection.execute(sa.text(
            "SELECT count(*) FROM app.litigation WHERE id_litigio=:id"
        ), {"id": business_id}).scalar_one() == 1
        assert connection.execute(sa.text(
            "SELECT count(*) FROM app.record_application WHERE source_record_id=:id"
        ), {"id": source_id}).scalar_one() == 1
        assert connection.execute(sa.text(
            "SELECT count(*) FROM app.kpi_observation WHERE kpi_code='KPI-LI-05' "
            "AND period_start='2026-04-01'"
        )).scalar_one() == 1
        assert connection.execute(sa.text("SELECT count(*) FROM app.analytic_run")).scalar_one() == runs_after
        assert connection.execute(sa.text("SELECT count(*) FROM audit.event")).scalar_one() == events_after
        assert connection.execute(sa.text(
            "SELECT to_jsonb(snapshot)::text FROM app.proactive_input_snapshot snapshot ORDER BY id"
        )).scalars().all() == historical
        job = connection.execute(sa.text(
            "SELECT * FROM app.job_run WHERE id=:id"
        ), {"id": first.job_id}).mappings().one()
        assert job["state"] == "COMPLETED" and job["safe_error_code"] is None
        run_ids = tuple(UUID(value) for value in job["result_references"]["analytic_run_ids"])
        assert run_ids
        assert all(row["state"] == "COMPLETED" and row["result"] == "SUCCESS" for row in
                   connection.execute(sa.text(
                       "SELECT state, result FROM app.analytic_run WHERE id = ANY(:ids)"
                   ), {"ids": list(run_ids)}).mappings())
        audit = connection.execute(sa.text(
            "SELECT action, result FROM audit.event WHERE correlation_id=:id"
        ), {"id": correlation_id}).mappings().all()
        assert {row["action"] for row in audit} >= {"QUARANTINE_REINJECT", "KPI_RECALCULATION"}
        assert not any(row["result"] == "FAILURE" for row in audit)
