"""Evidencia PostgreSQL de la ejecución proactiva programada."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.analytics.models import ProactiveAnalysisContext, ProactiveAnalysisError
from app.coordination.proactive_integration import (
    ProactiveIntegrationError,
    ProactiveIntegrationService,
    _trigger_references,
    derive_proactive_operation_id,
)
from app.coordination.proactive_repository import ProactiveIntegrationRepository
from app.security.repository import SecurityRepository


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _url() -> str:
    value = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("DATA_TEST_DATABASE_URL no configurada")
    parsed = make_url(value)
    if "test" not in (parsed.database or "").lower():
        pytest.fail("La evidencia requiere una base PostgreSQL de pruebas")
    return parsed.set(drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )


def _conninfo(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


@pytest.fixture
def database():
    url = _url()
    parsed = make_url(url)
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost",
        POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "",
        POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    engine = sa.create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE audit.event, app.proactive_input_snapshot, "
                "app.proactive_evaluation, app.job_run, app.kpi_observation, "
                "app.analytic_run CASCADE"
            )
        )
    try:
        yield engine, _conninfo(url)
    finally:
        engine.dispose()


def _interval(day: int = 1):
    start = datetime(2026, 8, day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _service(conninfo: str, audit=None) -> ProactiveIntegrationService:
    audit = audit or SecurityRepository(conninfo)
    return ProactiveIntegrationService(
        conninfo=conninfo,
        audit_repository=audit,
        repository=ProactiveIntegrationRepository(conninfo),
    )


def _seed_observation(engine, *, code: str = "KPI-RC-01") -> UUID:
    source_run, observation = uuid4(), uuid4()
    calculated = datetime(2026, 7, 31, 12, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """INSERT INTO app.analytic_run
                   (id, run_type, state, window_start, window_end, kpi_codes,
                    dimensions, rules_reference, result, operation_id,
                    correlation_id, completed_at)
                   VALUES (:id, 'KPI_RECALCULATION', 'COMPLETED', :at, :at,
                           ARRAY[:code], '{}'::jsonb, '{}'::jsonb, 'SUCCESS',
                           :operation, :correlation, :at)"""
            ),
            {
                "id": source_run,
                "at": calculated,
                "code": code,
                "operation": uuid4(),
                "correlation": uuid4(),
            },
        )
        connection.execute(
            sa.text(
                """INSERT INTO app.kpi_observation
                   (id, analytic_run_id, kpi_code, period_start, period_end,
                    dimensions, value, availability, calculated_at, as_of_date)
                   VALUES (:id, :run, :code, '2026-07-01', '2026-07-31',
                           '{}'::jsonb, :value, 'DISPONIBLE', :at, '2026-07-31')"""
            ),
            {
                "id": observation,
                "run": source_run,
                "code": code,
                "value": Decimal("10"),
                "at": calculated,
            },
        )
    return observation


def _create_started_job(service, start, end, correlation_id):
    operation_id = derive_proactive_operation_id(
        interval_start=start,
        interval_end=end,
    )
    with service.repository.transaction() as connection:
        service.repository.lock_operation(connection, operation_id)
        job_id = service.repository.create_job(
            connection,
            operation_id=operation_id,
            correlation_id=correlation_id,
            actor_process="controlled_ingestion.proactive_analysis",
            window_start=start,
            window_end=end,
            references=_trigger_references(start, end),
        )
    return {
        "id": job_id,
        "operation_id": operation_id,
        "correlation_id": correlation_id,
        "actor_process": "controlled_ingestion.proactive_analysis",
    }


def _freeze(service, job):
    return service.core._freeze(
        ProactiveAnalysisContext(
            job["id"],
            job["operation_id"],
            job["correlation_id"],
            job["actor_process"],
        )
    )


class _FailsBeforeFreeze:
    def execute(self, _context):
        raise ProactiveAnalysisError(
            "controlled failure",
            safe_cause_code="CONTROLLED_PRE_FREEZE_FAILURE",
        )


class _LosesCompletedResponse:
    def __init__(self, core) -> None:
        self.core = core

    def execute(self, context):
        self.core.execute(context)
        raise ProactiveAnalysisError(
            "response lost",
            safe_cause_code="RESPONSE_NOT_CONFIRMED",
        )


class _AuditFailure:
    def write_audit_event(self, *_args, **_kwargs):
        raise RuntimeError("controlled audit failure")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_relevant_update_completes_durable_job_and_core_run(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    start, end = _interval()
    outcome = _service(conninfo).schedule(
        interval_start=start,
        interval_end=end,
        correlation_id=uuid4(),
    )
    assert outcome.state == "COMPLETED"
    with engine.connect() as connection:
        job = connection.execute(
            sa.text("SELECT * FROM app.job_run WHERE id=:id"), {"id": outcome.job_id}
        ).mappings().one()
        assert job["case_type"] == "PROACTIVE_ANALYSIS"
        assert job["state"] == "COMPLETED"
        assert job["actor_process"] == "controlled_ingestion.proactive_analysis"
        assert job["result_references"]["contract"] == "SCHEDULED_PROACTIVE_ANALYSIS_V1"
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS' AND state='COMPLETED'")
        ).scalar_one() == 1
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.proactive_input_snapshot WHERE job_run_id=:id"),
            {"id": outcome.job_id},
        ).scalar_one() == 1
        assert connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE action='PROACTIVE_ANALYSIS' AND result='SUCCESS'")
        ).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_no_relevant_work_finalizes_atomically_with_one_audit(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    service = _service(conninfo)
    first_start, first_end = _interval()
    service.schedule(
        interval_start=first_start,
        interval_end=first_end,
        correlation_id=uuid4(),
    )
    start, end = _interval(2)
    outcome = service.schedule(
        interval_start=start,
        interval_end=end,
        correlation_id=uuid4(),
    )
    assert outcome.state == "NO_RELEVANT_WORK"
    with engine.connect() as connection:
        job = connection.execute(
            sa.text("SELECT * FROM app.job_run WHERE id=:id"), {"id": outcome.job_id}
        ).mappings().one()
        assert job["result_references"]["status"] == "NO_RELEVANT_WORK"
        assert len(job["result_references"]["input_fingerprint_sha256"]) == 64
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")
        ).scalar_one() == 1
        assert connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE resource_identifier=:id AND result='NO_RELEVANT_WORK'"),
            {"id": str(outcome.job_id)},
        ).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_no_relevant_work_audit_failure_rolls_back_job_finalization(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    start, end = _interval()
    _service(conninfo).schedule(
        interval_start=start,
        interval_end=end,
        correlation_id=uuid4(),
    )
    second_start, second_end = _interval(2)
    failing = _service(conninfo, _AuditFailure())
    with pytest.raises(ProactiveIntegrationError):
        failing.schedule(
            interval_start=second_start,
            interval_end=second_end,
            correlation_id=uuid4(),
        )
    operation_id = derive_proactive_operation_id(
        interval_start=second_start,
        interval_end=second_end,
    )
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT state FROM app.job_run WHERE operation_id=:id"),
            {"id": operation_id},
        ).scalar_one() == "STARTED"


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_pre_freeze_failure_finalizes_failed_with_audit(database) -> None:
    engine, conninfo = database
    service = _service(conninfo)
    service.core = _FailsBeforeFreeze()
    start, end = _interval()
    with pytest.raises(ProactiveIntegrationError):
        service.schedule(
            interval_start=start,
            interval_end=end,
            correlation_id=uuid4(),
        )
    operation_id = derive_proactive_operation_id(
        interval_start=start,
        interval_end=end,
    )
    with engine.connect() as connection:
        job = connection.execute(
            sa.text("SELECT * FROM app.job_run WHERE operation_id=:id"),
            {"id": operation_id},
        ).mappings().one()
        assert job["state"] == "FAILED"
        assert job["safe_error_code"] == "CONTROLLED_PRE_FREEZE_FAILURE"
        assert connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE resource_identifier=:id AND result='FAILURE'"),
            {"id": str(job["id"])},
        ).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_lost_response_after_core_completed_reconciles_without_duplicate_success_audit(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    service = _service(conninfo)
    service.core = _LosesCompletedResponse(service.core)
    start, end = _interval()
    outcome = service.schedule(
        interval_start=start,
        interval_end=end,
        correlation_id=uuid4(),
    )
    assert outcome.state == "COMPLETED"
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE action='PROACTIVE_ANALYSIS' AND result='SUCCESS'")
        ).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_post_freeze_completed_run_reconciles_job_completed(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    service = _service(conninfo)
    service.core = _LosesCompletedResponse(service.core)
    start, end = _interval()
    outcome = service.schedule(
        interval_start=start,
        interval_end=end,
        correlation_id=uuid4(),
    )
    with engine.connect() as connection:
        job = connection.execute(
            sa.text("SELECT * FROM app.job_run WHERE id=:id"), {"id": outcome.job_id}
        ).mappings().one()
        assert job["state"] == "COMPLETED"
        assert UUID(job["result_references"]["analytic_run_id"])


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_post_freeze_failed_run_reconciles_job_failed_without_duplicate_audit(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    service = _service(conninfo)
    start, end = _interval()
    job = _create_started_job(service, start, end, uuid4())
    run_id, _ = _freeze(service, job)
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE app.analytic_run SET state='FAILED', result='FAILURE', not_evaluated_reason='CONTROLLED_POST_FREEZE_FAILURE', completed_at=CURRENT_TIMESTAMP WHERE id=:id"),
            {"id": run_id},
        )
        SecurityRepository.write_audit_event(
            connection.connection.driver_connection,
            actor=None,
            action="PROACTIVE_ANALYSIS",
            resource_type="ANALYTIC_RUN",
            resource_identifier=str(run_id),
            result="FAILURE",
            correlation_id=job["correlation_id"],
            safe_cause_code="CONTROLLED_POST_FREEZE_FAILURE",
            process_identifier=job["actor_process"],
        )
    service.core = _FailsBeforeFreeze()
    with pytest.raises(ProactiveIntegrationError):
        service.schedule(
            interval_start=start,
            interval_end=end,
            correlation_id=uuid4(),
        )
    with engine.connect() as connection:
        persisted = connection.execute(
            sa.text("SELECT * FROM app.job_run WHERE id=:id"), {"id": job["id"]}
        ).mappings().one()
        assert persisted["state"] == "FAILED"
        assert persisted["safe_error_code"] == "CONTROLLED_POST_FREEZE_FAILURE"
        assert connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE action='PROACTIVE_ANALYSIS' AND result='FAILURE'")
        ).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_post_freeze_started_run_does_not_terminalize_job(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    service = _service(conninfo)
    start, end = _interval()
    job = _create_started_job(service, start, end, uuid4())
    _freeze(service, job)
    service.core = _FailsBeforeFreeze()
    with pytest.raises(ProactiveIntegrationError):
        service.schedule(
            interval_start=start,
            interval_end=end,
            correlation_id=uuid4(),
        )
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT state FROM app.job_run WHERE id=:id"), {"id": job["id"]}
        ).scalar_one() == "STARTED"
        assert connection.execute(
            sa.text("SELECT count(*) FROM audit.event WHERE resource_identifier=:id"),
            {"id": str(job["id"])},
        ).scalar_one() == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_core_failure_audit_not_confirmed_leaves_envelope_recoverable(database) -> None:
    test_post_freeze_started_run_does_not_terminalize_job(database)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_retry_preserves_initial_trigger_references_and_first_correlation(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    service = _service(conninfo)
    start, end = _interval()
    first_correlation = uuid4()
    first = service.schedule(
        interval_start=start,
        interval_end=end,
        correlation_id=first_correlation,
    )
    second = service.schedule(
        interval_start=start,
        interval_end=end,
        correlation_id=uuid4(),
    )
    assert first.job_id == second.job_id
    with engine.connect() as connection:
        job = connection.execute(
            sa.text("SELECT * FROM app.job_run WHERE id=:id"), {"id": first.job_id}
        ).mappings().one()
        assert job["correlation_id"] == first_correlation
        assert job["result_references"]["data_interval_start"] == start.isoformat()
        assert job["result_references"]["data_interval_end"] == end.isoformat()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_two_concurrent_dispatches_create_one_logical_job(database) -> None:
    engine, conninfo = database
    _seed_observation(engine)
    start, end = _interval()

    def execute():
        return _service(conninfo).schedule(
            interval_start=start,
            interval_end=end,
            correlation_id=uuid4(),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _item: execute(), range(2)))
    assert outcomes[0].job_id == outcomes[1].job_id
    assert {outcome.state for outcome in outcomes} == {"COMPLETED"}
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.job_run WHERE case_type='PROACTIVE_ANALYSIS'")
        ).scalar_one() == 1
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")
        ).scalar_one() == 1
