"""Lecturas reales sobre una base de pruebas ya migrada; cada caso revierte sus fixtures."""
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
import os
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest
from sqlalchemy.engine import make_url

from app.analytics.api import dashboard_service_for, router
from app.analytics.api_models import DashboardFilters
from app.analytics.dashboard_service import DashboardService
from app.analytics.models import KpiRecalculationContext, ProactiveContextReferenceResult, ProactiveEvaluationResult, ProactiveFindingResult, ProactiveQueryContext
from app.analytics.repository import AnalyticsRepository
from app.analytics.service import KpiQueryService, ProactiveAnalysisQueryService
from app.security.api import current_principal
from app.security.models import AuthenticatedPrincipal, SecurityError
from app.security.repository import SecurityRepository
from app.security.service import SecurityService


pytestmark = pytest.mark.requires_db


@pytest.fixture
def database():
    value = os.environ.get("DATA_TEST_DATABASE_URL", "")
    try:
        parsed = make_url(value)
        if "test" not in (parsed.database or "").lower():
            pytest.fail("Se requiere una base inequívocamente de pruebas")
        connection = psycopg.connect(parsed.set(drivername="postgresql").render_as_string(hide_password=False), row_factory=dict_row)
    except Exception:
        pytest.fail("La conexión de pruebas no está disponible", pytrace=False)
    try:
        assert "test" in connection.execute("SELECT current_database() AS name").fetchone()["name"].lower()
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone()["version_num"] == "0016_rag_operation_lifecycle"
        yield connection
    finally:
        connection.rollback()
        connection.close()


class BoundRepository(AnalyticsRepository):
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        with self.connection.transaction():
            yield self.connection


def principal(connection, role="JURIDICO"):
    account, session = uuid4(), uuid4()
    username = "dashboard-" + str(account)
    connection.execute("INSERT INTO app.user_account(id,username,display_name,password_hash) VALUES(%s,%s,'Lectura sintética','test-only-hash')", (account, username))
    connection.execute("INSERT INTO app.user_role(user_id,role_id,active) VALUES(%s,%s,true)", (account, role))
    connection.execute("INSERT INTO app.access_session(id,user_id,refresh_token_sha256,authorization_version,state,expires_at,correlation_id) VALUES(%s,%s,%s,1,'ACTIVE',%s,%s)", (session, account, uuid4().bytes + uuid4().bytes, datetime.now(UTC) + timedelta(hours=1), uuid4()))
    return AuthenticatedPrincipal(account, session, username, 1, frozenset({"TI"}), frozenset())


def dashboard(connection):
    repository = BoundRepository(connection)
    audit = SecurityRepository("unused")
    security = SecurityService(audit)
    return DashboardService(KpiQueryService(repository, security, audit), ProactiveAnalysisQueryService(repository, security, audit), today=lambda: date(2101, 1, 1))


def run(connection, *, state="COMPLETED", year=2100, run_type="PROACTIVE_ANALYSIS"):
    identity = uuid4()
    complete = None if state == "STARTED" else datetime(year, 2, 1, tzinfo=UTC)
    connection.execute("""INSERT INTO app.analytic_run(id,run_type,state,window_start,window_end,kpi_codes,rules_reference,result,operation_id,correlation_id,completed_at,executive_summary)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'Resumen persistido sin modificación.')""", (identity, run_type, state, datetime(year, 1, 1, tzinfo=UTC), datetime(year, 1, 31, 23, 59, tzinfo=UTC), [], Jsonb({"contract": "PROACTIVE_ANALYSIS_V1", "input_fingerprint_sha256": "a" * 64, "parent_job_run_id": str(uuid4()), "parent_operation_id": str(uuid4()), "signal_rules": []}), "PENDING" if state == "STARTED" else "SUCCESS" if state == "COMPLETED" else "FAILURE", uuid4(), uuid4(), complete))
    return identity


def observation(connection, run_id, *, code="KPI-LI-05", value=0, month=1):
    end = date(2100, month + 1, 1) - timedelta(days=1)
    connection.execute("""INSERT INTO app.kpi_observation(id,analytic_run_id,kpi_code,period_start,period_end,dimensions,value,availability,as_of_date,calculated_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (uuid4(), run_id, code, date(2100, month, 1), end, Jsonb({"test_series": str(uuid4())}), value, "NO_DISPONIBLE" if value is None else "DISPONIBLE", date(2100, month, 15), datetime(2100, month, 20, tzinfo=UTC)))


def context(actor):
    return KpiRecalculationContext(uuid4(), uuid4(), actor=actor)


def filters(**kwargs):
    return DashboardFilters(period="custom", period_start=date(2100, 1, 1), period_end=date(2100, 12, 31), **kwargs)


def test_kpi_read_preserves_values_and_never_creates_runs_or_jobs(database):
    actor = principal(database)
    identity = run(database, run_type="KPI_RECALCULATION")
    observation(database, identity)
    observation(database, identity, code="KPI-CN-02", value=None)
    observation(database, identity, code="KPI-CD-03", value=90)
    before = database.execute("SELECT (SELECT count(*) FROM app.analytic_run) AS runs,(SELECT count(*) FROM app.job_run) AS jobs,(SELECT count(*) FROM app.kpi_observation) AS observations").fetchone()
    page = dashboard(database).kpis(filters(), context(actor))
    assert {item.kpi_code: item.value for item in page.items} == {"KPI-LI-05": "0", "KPI-CN-02": None}
    after = database.execute("SELECT (SELECT count(*) FROM app.analytic_run) AS runs,(SELECT count(*) FROM app.job_run) AS jobs,(SELECT count(*) FROM app.kpi_observation) AS observations").fetchone()
    assert before == after


def test_rc03_reads_latest_valid_snapshot_instead_of_aggregating(database):
    actor = principal(database)
    identity = run(database, run_type="KPI_RECALCULATION")
    observation(database, identity, code="KPI-RC-03", value=3)
    observation(database, identity, code="KPI-RC-03", value=5, month=2)
    observation(database, identity, code="KPI-RC-03", value=None, month=3)
    page = dashboard(database).kpis(filters(), context(actor))
    assert len(page.items) == 1 and page.items[0].value == "5"


def test_default_reference_uses_last_complete_available_month(database):
    actor = principal(database)
    identity = run(database, run_type="KPI_RECALCULATION")
    observation(database, identity, month=5)
    observation(database, identity, code="KPI-CN-02", value=None, month=6)
    page = dashboard(database).kpis(DashboardFilters(), context(actor))
    assert (page.period_start, page.period_end, page.period_reference) == (date(2099, 12, 1), date(2100, 5, 31), date(2100, 5, 1))


@pytest.mark.parametrize("role", ["JURIDICO", "ANALISTA"])
def test_technical_route_rejects_non_ti_despite_caller_ti_claim(database, role):
    actor = principal(database, role)
    ctx = context(actor)
    with pytest.raises(SecurityError):
        dashboard(database).technical(ctx)
    rows = database.execute("SELECT action,result FROM audit.event WHERE correlation_id=%s", (ctx.correlation_id,)).fetchall()
    assert rows == [{"action": "AUTHORIZATION_DENIED", "result": "DENIED"}]


def test_ti_technical_read_and_null_missing_result(database):
    actor = principal(database, "TI")
    identity = run(database, run_type="KPI_RECALCULATION")
    observation(database, identity, code="KPI-CD-03", value=0)
    value = dashboard(database).technical(context(actor), period_start=date(2100, 1, 1), period_end=date(2100, 12, 31))
    assert value.value == "0"
    missing = dashboard(database).technical(context(actor), period_start=date(2200, 1, 1))
    assert missing.value is None and missing.calculated_at is None


def test_revoked_session_denies_read_and_preserves_denial_audit(database):
    actor = principal(database)
    database.execute("UPDATE app.access_session SET state='INVALIDATED', invalidated_at=CURRENT_TIMESTAMP WHERE id=%s", (actor.session_id,))
    ctx = context(actor)
    with pytest.raises(SecurityError):
        dashboard(database).kpis(filters(), ctx)
    assert database.execute("SELECT count(*) AS total FROM audit.event WHERE correlation_id=%s AND action='AUTHORIZATION_DENIED'", (ctx.correlation_id,)).fetchone()["total"] == 1


def test_analysis_current_ignores_newer_failed_and_started_runs(database):
    actor = principal(database)
    completed = run(database)
    run(database, state="FAILED", year=2102)
    run(database, state="STARTED", year=2103)
    page = dashboard(database).analysis(DashboardFilters(), ProactiveQueryContext(uuid4(), uuid4(), actor))
    assert page.current_analytic_run_id == completed
    assert len(page.items) == 1 and page.alert_count == 0
    assert page.items[0].executive_summary == "Resumen persistido sin modificación."
    assert page.items[0].completed_at == datetime(2100, 2, 1, tzinfo=UTC)


def test_analysis_history_and_specific_completed_run_are_read_only(database):
    actor = principal(database)
    first = run(database, year=2099)
    latest = run(database)
    count = database.execute("SELECT count(*) AS total FROM app.analytic_run").fetchone()["total"]
    page = dashboard(database).analysis(DashboardFilters(), ProactiveQueryContext(uuid4(), uuid4(), actor), analytic_run_id=first)
    assert [item.analytic_run_id for item in page.items] == [first]
    assert page.current_analytic_run_id == latest
    assert database.execute("SELECT count(*) AS total FROM app.analytic_run").fetchone()["total"] == count


def test_dashboard_http_real_authorization_and_safe_output(database):
    actor = principal(database)
    identity = run(database, run_type="KPI_RECALCULATION")
    observation(database, identity)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_principal] = lambda: actor
    app.dependency_overrides[dashboard_service_for] = lambda: dashboard(database)
    with TestClient(app) as client:
        response = client.get("/api/dashboard/kpis?period=custom&period_start=2100-01-01&period_end=2100-12-31")
        assert response.status_code == 200 and response.json()["items"][0]["value"] == "0"
        response = client.get("/api/technical/kpis/KPI-CD-03")
        assert response.status_code == 403 and response.json() == {"detail": "Acceso no autorizado"}


def test_analysis_exposes_finding_timestamp_context_and_filtered_alert_count(database):
    actor = principal(database)
    identity = run(database)
    evaluation = ProactiveEvaluationResult(uuid4(), identity, "KPI-CN-02", {}, date(2100, 1, 1), "EVALUATED", True, None, 0, 5, 0, 5, None, ("CN02_APPEARANCE",), None)
    AnalyticsRepository.persist_proactive_evaluation(database, evaluation)
    finding = ProactiveFindingResult(uuid4(), identity, evaluation.id, "KPI-CN-02", {}, date(2100, 1, 1), date(2100, 1, 31), 5, 0, 5, "CN02_APPEARANCE", None, "Descripción persistida.")
    AnalyticsRepository.persist_proactive_finding(database, finding)
    reference = ProactiveContextReferenceResult(uuid4(), finding.id, "KPI-CN-02", {}, finding.period_start, finding.period_end, {"proactive_evaluation_id": str(evaluation.id)}, uuid4(), uuid4())
    AnalyticsRepository.persist_context_reference(database, reference)
    page = dashboard(database).analysis(DashboardFilters(), ProactiveQueryContext(uuid4(), uuid4(), actor))
    assert page.alert_count == 1 and page.items[0].findings[0].created_at is not None
    assert page.items[0].findings[0].current_value == "5"
    assert page.items[0].context_references[0].finding_id == finding.id
    filtered = dashboard(database).analysis(DashboardFilters(risk_type="litigation"), ProactiveQueryContext(uuid4(), uuid4(), actor))
    assert filtered.alert_count == 0 and not filtered.items[0].context_references
    assert filtered.items[0].executive_summary == "Resumen persistido sin modificación."


def test_analysis_revalidates_dashboard_permission_and_audits_denial(database):
    actor = principal(database)
    database.execute("UPDATE app.access_session SET state='INVALIDATED', invalidated_at=CURRENT_TIMESTAMP WHERE id=%s", (actor.session_id,))
    ctx = ProactiveQueryContext(uuid4(), uuid4(), actor)
    with pytest.raises(SecurityError):
        dashboard(database).analysis(DashboardFilters(), ctx)
    assert database.execute("SELECT count(*) AS total FROM audit.event WHERE correlation_id=%s AND action='AUTHORIZATION_DENIED'", (ctx.correlation_id,)).fetchone()["total"] == 1


def test_analysis_completed_tie_break_uses_id_descending(database):
    actor = principal(database)
    first, second = run(database), run(database)
    page = dashboard(database).analysis(DashboardFilters(), ProactiveQueryContext(uuid4(), uuid4(), actor))
    assert page.current_analytic_run_id == max(first, second)


def test_insufficient_history_remains_an_evaluation_not_an_alert(database):
    actor = principal(database)
    identity = run(database)
    evaluation = ProactiveEvaluationResult(uuid4(), identity, "KPI-LI-05", {}, date(2100, 1, 1), "INSUFFICIENT_HISTORY", False, None, None, None, None, None, None, ("LI05_STRICT_INCREASE",), "MISSING_IMMEDIATE_MONTH")
    AnalyticsRepository.persist_proactive_evaluation(database, evaluation)
    page = dashboard(database).analysis(DashboardFilters(), ProactiveQueryContext(uuid4(), uuid4(), actor))
    assert page.alert_count == 0 and not page.items[0].findings
    assert page.items[0].evaluations[0].outcome == "INSUFFICIENT_HISTORY"
    assert page.items[0].evaluations[0].current_value is None
    assert not page.items[0].evaluations[0].signal_detected
