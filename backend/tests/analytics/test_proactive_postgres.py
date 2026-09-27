from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.analytics.models import (
    ProactiveAnalysisContext,
    ProactiveAnalysisError,
    ProactiveAnalysisQuery,
    ProactiveQueryContext,
)
from app.analytics.repository import AnalyticsRepository
from app.analytics.service import ProactiveAnalysisQueryService, ProactiveAnalysisService
from app.security.models import AuthenticatedPrincipal, AuthorizationError
from app.security.repository import SecurityRepository
from app.security.service import SecurityService


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _url() -> str:
    value = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not value or "test" not in (make_url(value).database or "").lower():
        pytest.fail("La evidencia requiere una base PostgreSQL desechable")
    return make_url(value).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


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
        connection.execute(sa.text(
            "TRUNCATE audit.event, app.job_run, app.kpi_observation, app.analytic_run CASCADE"
        ))
    try:
        yield engine, make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    finally:
        engine.dispose()


def _rows(engine, sql: str, **parameters):
    with engine.connect() as connection:
        return connection.execute(sa.text(sql), parameters).mappings().all()


def _job(engine, *, process: str = "controlled_ingestion.proactive_analysis"):
    job_id, operation_id, correlation_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text(
            """INSERT INTO app.job_run
               (id, case_type, state, actor_process, result_references, operation_id, correlation_id)
               VALUES (:id, 'PROACTIVE_ANALYSIS', 'STARTED', :process, '{}'::jsonb, :operation, :correlation)"""
        ), {"id": job_id, "process": process, "operation": operation_id, "correlation": correlation_id})
    return {
        "id": job_id,
        "operation_id": operation_id,
        "correlation_id": correlation_id,
        "actor_process": process,
    }


def _context(job) -> ProactiveAnalysisContext:
    return ProactiveAnalysisContext(job["id"], job["operation_id"], job["correlation_id"], job["actor_process"])


def _observation(engine, code: str, month: int, value: str | None, *, dimensions=None, calculated_at=None):
    run_id, observation_id = uuid4(), uuid4()
    period_start = date(2026, month, 1)
    period_end = date(2026, month + 1, 1) - timedelta(days=1) if month < 12 else date(2026, 12, 31)
    calculated_at = calculated_at or datetime(2026, month, 15, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(sa.text(
            """INSERT INTO app.analytic_run
               (id, run_type, state, window_start, window_end, kpi_codes, dimensions,
                rules_reference, result, operation_id, correlation_id, completed_at)
               VALUES (:id, 'KPI_RECALCULATION', 'COMPLETED', :start, :end, ARRAY[:code],
                       '{}'::jsonb, '{}'::jsonb, 'SUCCESS', :operation, :correlation, :completed)"""
        ), {"id": run_id, "start": calculated_at, "end": calculated_at, "code": code,
            "operation": uuid4(), "correlation": uuid4(), "completed": calculated_at})
        connection.execute(sa.text(
            """INSERT INTO app.kpi_observation
               (id, analytic_run_id, kpi_code, period_start, period_end, dimensions,
                value, availability, calculated_at, as_of_date)
               VALUES (:id, :run, :code, :start, :end, CAST(:dimensions AS jsonb),
                       :value, :availability, :calculated, :as_of)"""
        ), {"id": observation_id, "run": run_id, "code": code, "start": period_start,
            "end": period_end, "dimensions": __import__("json").dumps(dimensions or {}, sort_keys=True),
            "value": None if value is None else Decimal(value),
            "availability": "NO_DISPONIBLE" if value is None else "DISPONIBLE",
            "calculated": calculated_at, "as_of": period_start})
    return observation_id


def _recalculate_observation(engine, observation_id, *, calculated_at: datetime) -> None:
    source_run_id = uuid4()
    with engine.begin() as connection:
        connection.execute(sa.text(
            """INSERT INTO app.analytic_run
               (id, run_type, state, window_start, window_end, kpi_codes, dimensions,
                rules_reference, result, operation_id, correlation_id, completed_at)
               VALUES (:id, 'KPI_RECALCULATION', 'COMPLETED', :at, :at, ARRAY['KPI-RC-03'],
                       '{}'::jsonb, '{}'::jsonb, 'SUCCESS', :operation, :correlation, :at)"""
        ), {"id": source_run_id, "at": calculated_at, "operation": uuid4(), "correlation": uuid4()})
        connection.execute(sa.text(
            """UPDATE app.kpi_observation
                  SET analytic_run_id=:run, calculated_at=:at
                WHERE id=:id"""
        ), {"run": source_run_id, "at": calculated_at, "id": observation_id})


def _historical_recalculation_scenario(database):
    engine, conninfo = database
    historical_id = None
    for month in range(1, 8):
        observation_id = _observation(engine, "KPI-RC-03", month, str(month))
        if month == 1:
            historical_id = observation_id

    first = _service(conninfo).execute(_context(_job(engine)))
    assert first.status == "COMPLETED"
    _recalculate_observation(
        engine,
        historical_id,
        calculated_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    second = _service(conninfo).execute(_context(_job(engine)))
    third = _service(conninfo).execute(_context(_job(engine)))
    return engine, historical_id, first, second, third


def _service(conninfo: str, audit=None) -> ProactiveAnalysisService:
    return ProactiveAnalysisService(AnalyticsRepository(conninfo), audit or SecurityRepository(conninfo))


@pytest.mark.requires_db
def test_historical_recalculation_outside_closure_creates_completed_run(database) -> None:
    engine, historical_id, first, second, _ = _historical_recalculation_scenario(database)
    assert second.status == "COMPLETED"
    assert second.analytic_run_id != first.analytic_run_id
    assert second.input_fingerprint_sha256 != first.input_fingerprint_sha256
    evaluations = _rows(engine, "SELECT evaluation_period FROM app.proactive_evaluation WHERE analytic_run_id=:id", id=second.analytic_run_id)
    assert evaluations == [{"evaluation_period": date(2026, 7, 1)}]
    snapshot = _rows(
        engine,
        "SELECT source_observation_id, period_start FROM app.proactive_input_snapshot WHERE analytic_run_id=:id ORDER BY period_start",
        id=second.analytic_run_id,
    )
    assert [row["period_start"].month for row in snapshot] == [3, 4, 5, 6, 7]
    assert historical_id not in {row["source_observation_id"] for row in snapshot}


@pytest.mark.requires_db
def test_historical_recalculation_advances_completed_fingerprint(database) -> None:
    engine, _, first, second, _ = _historical_recalculation_scenario(database)
    assert second.input_fingerprint_sha256 != first.input_fingerprint_sha256
    persisted = _rows(
        engine,
        "SELECT rules_reference FROM app.analytic_run WHERE id=:id",
        id=second.analytic_run_id,
    )[0]["rules_reference"]
    assert persisted["input_fingerprint_sha256"] == second.input_fingerprint_sha256


@pytest.mark.requires_db
def test_historical_recalculation_then_unchanged_retry_is_no_relevant_work(database) -> None:
    engine, _, _, second, third = _historical_recalculation_scenario(database)
    assert second.status == "COMPLETED"
    assert third.status == "NO_RELEVANT_WORK"
    assert third.input_fingerprint_sha256 == second.input_fingerprint_sha256
    assert len(_rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")) == 2


@pytest.mark.requires_db
def test_context_only_rc01_update_is_relevant(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 1, "4")
    first_job = _job(engine)
    first = _service(conninfo).execute(_context(first_job))

    assert first.status == "COMPLETED"
    assert first.evaluations == () and first.findings == ()
    assert first.executive_summary == "Análisis completado sin hallazgos."
    run = _rows(engine, "SELECT state, result, kpi_codes, rules_reference FROM app.analytic_run WHERE id=:id", id=first.analytic_run_id)[0]
    assert run["state"] == "COMPLETED" and run["result"] == "SUCCESS" and run["kpi_codes"] == []
    assert run["rules_reference"]["input_fingerprint_sha256"] == first.input_fingerprint_sha256
    assert _rows(engine, "SELECT id FROM app.proactive_evaluation") == []
    assert _rows(engine, "SELECT id FROM app.finding") == []
    assert _rows(engine, "SELECT id FROM app.context_reference") == []

    second_job = _job(engine)
    second = _service(conninfo).execute(_context(second_job))
    assert second.status == "NO_RELEVANT_WORK"
    assert len(_rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")) == 1
    assert _rows(engine, "SELECT state FROM app.job_run WHERE id=:id", id=second_job["id"])[0]["state"] == "STARTED"
    assert _rows(engine, "SELECT id FROM audit.event WHERE correlation_id=:id", id=second_job["correlation_id"]) == []


@pytest.mark.requires_db
def test_context_only_eo01_update_is_relevant(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-EO-01", 2, "3", dimensions={"tipo_asunto": "Litigio", "estado": "Abierto"})
    job = _job(engine)
    result = _service(conninfo).execute(_context(job))
    assert result.status == "COMPLETED" and result.evaluations == () and result.findings == ()
    snapshot = _rows(engine, "SELECT kpi_code FROM app.proactive_input_snapshot")
    assert snapshot == [{"kpi_code": "KPI-EO-01"}]


@pytest.mark.requires_db
def test_context_only_relevant_run_persists_completed_fingerprint(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 1, "4")
    result = _service(conninfo).execute(_context(_job(engine)))
    run = _rows(engine, "SELECT state, rules_reference FROM app.analytic_run WHERE id=:id", id=result.analytic_run_id)[0]
    assert run["state"] == "COMPLETED"
    assert run["rules_reference"]["input_fingerprint_sha256"] == result.input_fingerprint_sha256


@pytest.mark.requires_db
def test_context_only_relevant_run_has_zero_evaluations_and_findings(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-EO-01", 1, "2")
    result = _service(conninfo).execute(_context(_job(engine)))
    assert result.status == "COMPLETED"
    assert _rows(engine, "SELECT id FROM app.proactive_evaluation") == []
    assert _rows(engine, "SELECT id FROM app.finding") == []
    assert _rows(engine, "SELECT id FROM app.context_reference") == []


@pytest.mark.requires_db
def test_context_only_completed_run_advances_relevance_baseline(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 1, "1")
    first = _service(conninfo).execute(_context(_job(engine)))
    _observation(engine, "KPI-RC-01", 2, "1")
    second = _service(conninfo).execute(_context(_job(engine)))
    assert second.status == "COMPLETED"
    assert second.input_fingerprint_sha256 != first.input_fingerprint_sha256
    latest = _rows(engine, "SELECT rules_reference FROM app.analytic_run WHERE id=:id", id=second.analytic_run_id)[0]
    assert latest["rules_reference"]["input_fingerprint_sha256"] == second.input_fingerprint_sha256


@pytest.mark.requires_db
def test_same_context_only_input_next_execution_is_no_relevant_work(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-EO-01", 1, "2")
    first = _service(conninfo).execute(_context(_job(engine)))
    second = _service(conninfo).execute(_context(_job(engine)))
    assert first.status == "COMPLETED"
    assert second.status == "NO_RELEVANT_WORK"
    assert len(_rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")) == 1


@pytest.mark.requires_db
def test_first_execution_with_only_context_observations_is_relevant(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 1, "3")
    result = _service(conninfo).execute(_context(_job(engine)))
    assert result.status == "COMPLETED"
    assert result.evaluations == () and result.findings == ()


@pytest.mark.requires_db
def test_first_execution_with_no_eligible_observations_is_no_relevant_work(database) -> None:
    engine, conninfo = database
    job = _job(engine)
    result = _service(conninfo).execute(_context(job))
    assert result.status == "NO_RELEVANT_WORK"
    assert _rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'") == []
    assert _rows(engine, "SELECT id FROM audit.event WHERE correlation_id=:id", id=job["correlation_id"]) == []


@pytest.mark.requires_db
def test_completed_run_persists_canonical_fingerprint_and_fresh_instance_reads_it(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-03", 1, "0")
    _observation(engine, "KPI-RC-03", 2, "1")
    first = _service(conninfo).execute(_context(_job(engine)))
    persisted = _rows(engine, "SELECT rules_reference FROM app.analytic_run WHERE id=:id", id=first.analytic_run_id)[0]["rules_reference"]
    assert persisted["input_fingerprint_sha256"] == first.input_fingerprint_sha256
    second = _service(conninfo).execute(_context(_job(engine)))
    assert second.status == "NO_RELEVANT_WORK"


@pytest.mark.requires_db
def test_invalid_latest_completed_fingerprint_fails_closed(database) -> None:
    engine, conninfo = database
    at = datetime(2026, 1, 31, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(sa.text(
            """INSERT INTO app.analytic_run
               (id, run_type, state, window_start, window_end, kpi_codes, dimensions,
                rules_reference, result, operation_id, correlation_id, completed_at)
               VALUES (:id, 'PROACTIVE_ANALYSIS', 'COMPLETED', :at, :at, ARRAY[]::text[],
                       '{}'::jsonb, '{"contract":"PROACTIVE_ANALYSIS_V1"}'::jsonb,
                       'SUCCESS', :operation, :correlation, :at)"""
        ), {"id": uuid4(), "operation": uuid4(), "correlation": uuid4(), "at": at})
    _observation(engine, "KPI-RC-01", 1, "2")
    with pytest.raises(ProactiveAnalysisError) as error:
        _service(conninfo).execute(_context(_job(engine)))
    assert error.value.safe_cause_code == "PROACTIVE_FINGERPRINT_CONTRACT_GAP"
    assert len(_rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")) == 1


@pytest.mark.requires_db
def test_latest_no_disponible_month_is_persisted_as_current_insufficient_history(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-03", 1, "3")
    _observation(engine, "KPI-RC-03", 2, None)
    result = _service(conninfo).execute(_context(_job(engine)))
    assert len(result.evaluations) == 1
    evaluation = result.evaluations[0]
    assert evaluation.evaluation_period == date(2026, 2, 1)
    assert evaluation.outcome == "INSUFFICIENT_HISTORY"
    row = _rows(engine, "SELECT evaluation_period, outcome FROM app.proactive_evaluation")[0]
    assert row == {"evaluation_period": date(2026, 2, 1), "outcome": "INSUFFICIENT_HISTORY"}


@pytest.mark.requires_db
def test_signal_persists_one_evaluation_finding_reference_and_process_audit(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-03", 1, "0")
    _observation(engine, "KPI-RC-03", 2, "5")
    job = _job(engine, process="daily-proactive-analysis")
    result = _service(conninfo).execute(_context(job))
    assert len(result.evaluations) == len(result.findings) == 1
    assert len(_rows(engine, "SELECT id FROM app.proactive_evaluation")) == 1
    assert len(_rows(engine, "SELECT id FROM app.finding")) == 1
    references = _rows(engine, "SELECT finding_id, kpi_code, dimensions, period_start, period_end, context_identifiers FROM app.context_reference")
    assert len(references) == 1
    finding = _rows(engine, "SELECT id, kpi_code, dimensions, period_start, period_end FROM app.finding")[0]
    assert references[0]["finding_id"] == finding["id"]
    assert references[0]["kpi_code"] == finding["kpi_code"]
    assert references[0]["dimensions"] == finding["dimensions"]
    assert references[0]["period_start"] == finding["period_start"]
    assert references[0]["period_end"] == finding["period_end"]
    assert set(references[0]["context_identifiers"]) == {"proactive_evaluation_id", "source_observation_ids", "terminal_months_used"}
    audit = _rows(engine, "SELECT actor_type, actor_identifier, action, result, correlation_id FROM audit.event")[0]
    assert audit == {"actor_type": "PROCESS", "actor_identifier": "daily-proactive-analysis", "action": "PROACTIVE_ANALYSIS", "result": "SUCCESS", "correlation_id": job["correlation_id"]}


@pytest.mark.requires_db
def test_context_mismatch_creates_no_effects_or_audit(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 1, "2")
    job = _job(engine)
    bad = ProactiveAnalysisContext(job["id"], job["operation_id"], job["correlation_id"], "caller-process")
    with pytest.raises(ProactiveAnalysisError) as error:
        _service(conninfo).execute(bad)
    assert error.value.safe_cause_code == "JOB_PROCESS_MISMATCH"
    assert _rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'") == []
    assert _rows(engine, "SELECT id FROM audit.event") == []


class _FailingAudit:
    def write_audit_event(self, connection, **kwargs):
        SecurityRepository.write_audit_event(connection, **kwargs)
        raise RuntimeError("audit unavailable")


@pytest.mark.requires_db
def test_mandatory_audit_failure_rolls_back_successful_analysis_effects(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-03", 1, "0")
    _observation(engine, "KPI-RC-03", 2, "1")
    with pytest.raises(ProactiveAnalysisError):
        _service(conninfo, _FailingAudit()).execute(_context(_job(engine)))
    assert _rows(engine, "SELECT id FROM app.proactive_evaluation") == []
    assert _rows(engine, "SELECT id FROM app.finding") == []
    assert _rows(engine, "SELECT id FROM app.context_reference") == []
    assert _rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS' AND state='COMPLETED'") == []
    assert _rows(engine, "SELECT id FROM audit.event") == []


class _FailOnceAudit:
    def __init__(self):
        self.failed = False

    def write_audit_event(self, connection, **kwargs):
        SecurityRepository.write_audit_event(connection, **kwargs)
        if kwargs["result"] == "SUCCESS" and not self.failed:
            self.failed = True
            raise RuntimeError("temporary audit failure")


@pytest.mark.requires_db
def test_failed_retry_reuses_snapshot_and_identity(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-03", 1, "0")
    _observation(engine, "KPI-RC-03", 2, "1")
    job, audit = _job(engine), _FailOnceAudit()
    with pytest.raises(ProactiveAnalysisError):
        _service(conninfo, audit).execute(_context(job))
    failed = _rows(engine, "SELECT id, operation_id, state FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")[0]
    snapshot_ids = _rows(engine, "SELECT id FROM app.proactive_input_snapshot ORDER BY id")
    assert failed["state"] == "FAILED" and snapshot_ids

    completed = _service(conninfo).execute(_context(job))
    assert completed.analytic_run_id == failed["id"]
    assert _rows(engine, "SELECT id FROM app.proactive_input_snapshot ORDER BY id") == snapshot_ids
    assert _rows(engine, "SELECT state FROM app.analytic_run WHERE id=:id", id=failed["id"])[0]["state"] == "COMPLETED"


@pytest.mark.requires_db
def test_mixed_evaluation_outcomes_keep_run_reason_null_and_deterministic_window(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-03", 1, "0")
    _observation(engine, "KPI-RC-03", 2, "1")
    _observation(engine, "KPI-LI-05", 3, "1")
    result = _service(conninfo).execute(_context(_job(engine)))
    assert {item.outcome for item in result.evaluations} == {"EVALUATED", "INSUFFICIENT_HISTORY"}
    run = _rows(engine, "SELECT window_start, window_end, kpi_codes, not_evaluated_reason FROM app.analytic_run WHERE id=:id", id=result.analytic_run_id)[0]
    assert run["window_start"].date() == date(2025, 10, 1)
    assert run["window_end"].date() == date(2026, 3, 31)
    assert run["kpi_codes"] == ["KPI-LI-05", "KPI-RC-03"]
    assert run["not_evaluated_reason"] is None


@pytest.mark.requires_db
def test_context_only_relevant_run_does_not_create_cross_kpi_associations(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 2, "9")
    signal_ids = {
        _observation(engine, "KPI-RC-03", 1, "0"),
        _observation(engine, "KPI-RC-03", 2, "3"),
    }
    result = _service(conninfo).execute(_context(_job(engine)))
    assert len(result.findings) == 1 and "KPI-RC-01" not in result.executive_summary
    identifiers = _rows(engine, "SELECT context_identifiers FROM app.context_reference")[0]["context_identifiers"]
    assert set(identifiers["source_observation_ids"]) == {str(item) for item in signal_ids}


@pytest.mark.requires_db
def test_concurrent_same_job_and_input_does_not_duplicate_effects(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-03", 1, "0")
    _observation(engine, "KPI-RC-03", 2, "2")
    job, barrier = _job(engine), Barrier(2)

    def invoke():
        barrier.wait(timeout=10)
        return _service(conninfo).execute(_context(job))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=30) for future in [executor.submit(invoke), executor.submit(invoke)]]
    assert results[0].analytic_run_id == results[1].analytic_run_id
    assert len(_rows(engine, "SELECT id FROM app.analytic_run WHERE run_type='PROACTIVE_ANALYSIS'")) == 1
    assert len(_rows(engine, "SELECT id FROM app.proactive_evaluation")) == 1
    assert len(_rows(engine, "SELECT id FROM app.finding")) == 1
    assert len(_rows(engine, "SELECT id FROM app.context_reference")) == 1


class _SecuritySpy:
    def __init__(self):
        self.capabilities = []

    def revalidate_functional_access(self, connection, actor, capability):
        self.capabilities.append(capability)
        return actor


@pytest.mark.requires_db
def test_query_uses_dashboard_read_and_failed_run_does_not_replace_completed(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 1, "2")
    completed = _service(conninfo).execute(_context(_job(engine)))
    at = datetime(2026, 2, 1, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(sa.text(
            """INSERT INTO app.analytic_run
               (id, run_type, state, window_start, window_end, kpi_codes, dimensions,
                rules_reference, result, not_evaluated_reason, operation_id, correlation_id, completed_at)
               VALUES (:id, 'PROACTIVE_ANALYSIS', 'FAILED', :at, :at, ARRAY[]::text[],
                       '{}'::jsonb, '{}'::jsonb, 'FAILURE', 'TEST_FAILURE', :operation, :correlation, :at)"""
        ), {"id": uuid4(), "operation": uuid4(), "correlation": uuid4(), "at": at})
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), "reader", 1, frozenset(), frozenset({"dashboard.read"}))
    security = _SecuritySpy()
    query = ProactiveAnalysisQueryService(AnalyticsRepository(conninfo), security, SecurityRepository(conninfo))
    result = query.latest_completed(ProactiveQueryContext(uuid4(), uuid4(), principal))
    assert result is not None and result.result.analytic_run_id == completed.analytic_run_id
    assert security.capabilities == ["dashboard.read"]


def _principal(engine, *, role: str | None) -> AuthenticatedPrincipal:
    account_id, session_id = uuid4(), uuid4()
    username = f"proactive-{account_id}"
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (:id, :username, 'Proactive', 'test-only-hash')"
        ), {"id": account_id, "username": username})
        if role:
            connection.execute(sa.text(
                "INSERT INTO app.user_role (user_id, role_id, active) VALUES (:id, :role, true)"
            ), {"id": account_id, "role": role})
        connection.execute(sa.text(
            """INSERT INTO app.access_session
               (id, user_id, refresh_token_sha256, authorization_version, state, expires_at, correlation_id)
               VALUES (:id, :user, :digest, 1, 'ACTIVE', :expires, :correlation)"""
        ), {"id": session_id, "user": account_id, "digest": uuid4().bytes + uuid4().bytes,
            "expires": datetime.now(UTC) + timedelta(hours=1), "correlation": uuid4()})
    return AuthenticatedPrincipal(account_id, session_id, username, 1, frozenset(), frozenset())


@pytest.mark.requires_db
def test_proactive_query_revalidates_dashboard_read(database) -> None:
    engine, conninfo = database
    _observation(engine, "KPI-RC-01", 1, "2")
    completed = _service(conninfo).execute(_context(_job(engine)))
    repository = AnalyticsRepository(conninfo)
    service = ProactiveAnalysisQueryService(repository, SecurityService(SecurityRepository(conninfo)), SecurityRepository(conninfo))

    allowed = _principal(engine, role="JURIDICO")
    visible = service.latest_completed(ProactiveQueryContext(uuid4(), uuid4(), allowed))
    assert visible is not None and visible.result.analytic_run_id == completed.analytic_run_id

    denied = _principal(engine, role=None)
    with pytest.raises(AuthorizationError):
        service.list_completed(ProactiveAnalysisQuery(), ProactiveQueryContext(uuid4(), uuid4(), denied))
