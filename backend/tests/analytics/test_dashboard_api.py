"""Pruebas de transporte y selección, sin sustituir la evidencia PostgreSQL."""
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.analytics.api import dashboard_service_for, router
from app.analytics.api_models import DashboardFilters
from app.analytics.dashboard_service import DashboardService, period_bounds
from app.analytics.models import KpiRecalculationContext, ProactiveAnalysisReadResult, ProactiveAnalysisResult, ProactiveQueryContext
from app.analytics.service import KpiQueryService
from app.security.api import current_principal
from app.security.models import AuthenticatedPrincipal, AuthenticationError, AuthorizationError


def actor(role="JURIDICO"):
    return AuthenticatedPrincipal(uuid4(), uuid4(), "lectura", 1, frozenset({role}), frozenset())


class Repository:
    def __init__(self, rows):
        self.rows = rows
        self.reads = 0

    @contextmanager
    def transaction(self):
        yield self

    def list_observations(self, connection, *, kpi_code, **kwargs):
        self.reads += 1
        return [row for row in self.rows if kpi_code is None or row["kpi_code"] == kpi_code]


class Security:
    def __init__(self, principal=None, denied=False):
        self.principal = principal or actor()
        self.denied = denied
        self.capabilities = []

    def revalidate_functional_access(self, connection, supplied, capability):
        self.capabilities.append(capability)
        if self.denied:
            raise AuthorizationError("denegado")
        return self.principal


class Audit:
    def __init__(self):
        self.events = []

    def write_audit_event(self, connection, **event):
        self.events.append(event)


def row(code="KPI-LI-05", month=1, value=Decimal(0), availability="DISPONIBLE", dimensions=None):
    end = date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1)
    from datetime import timedelta
    return dict(id=uuid4(), kpi_code=code, period_start=date(2026, month, 1), period_end=end - timedelta(days=1), as_of_date=date(2026, month, 15), calculated_at=datetime(2026, month, 20, tzinfo=UTC), dimensions=dimensions or {}, value=value, availability=availability)


def service(rows, security=None):
    repo, audit = Repository(rows), Audit()
    security = security or Security()
    return DashboardService(KpiQueryService(repo, security, audit), None, today=lambda: date(2026, 8, 1)), repo, audit, security


def client(dashboard, *, authenticated=True):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dashboard_service_for] = lambda: dashboard
    if authenticated:
        app.dependency_overrides[current_principal] = actor
    return TestClient(app)


def test_kpi_dto_preserves_zero_unavailable_and_excludes_technical():
    dashboard, repo, audit, security = service([row(), row("KPI-CN-02", value=None, availability="NO_DISPONIBLE"), row("KPI-CD-03")])
    response = client(dashboard).get("/api/dashboard/kpis")
    assert response.status_code == 200
    body = response.json()
    assert body["period_reference"] == "2026-01-01"
    assert {item["kpi_code"]: item["value"] for item in body["items"]} == {"KPI-LI-05": "0", "KPI-CN-02": None}
    assert all(not item["entity_filter_applicable"] for item in body["items"])
    assert security.capabilities == ["kpi.read", "dashboard.read"]
    assert repo.reads == 1 and not audit.events


def test_rc03_uses_latest_valid_observation_not_sum():
    dashboard, *_ = service([row("KPI-RC-03", 1, Decimal(7)), row("KPI-RC-03", 2, Decimal(11)), row("KPI-RC-03", 3, None, "NO_DISPONIBLE")])
    response = client(dashboard).get("/api/dashboard/kpis?period=custom&period_start=2026-01-01&period_end=2026-03-31")
    assert response.status_code == 200
    assert [(item["value"], item["period_start"]) for item in response.json()["items"]] == [("11", "2026-02-01")]


def test_entity_does_not_infer_area_or_filter_non_applicable_series():
    dashboard, *_ = service([row("KPI-CN-03", dimensions={"area": "Legal"}), row(dimensions={"entity": "A"}), row(dimensions={"entity": "B"})])
    body = client(dashboard).get("/api/dashboard/kpis?entity=A").json()
    assert len(body["items"]) == 2
    assert {item["dimensions"].get("entity") for item in body["items"]} == {None, "A"}


def test_risk_filter_preserves_context_without_inventing_classification():
    dashboard, *_ = service([row("KPI-LI-05"), row("KPI-CN-02"), row("KPI-EO-01"), row("KPI-RC-01")])
    body = client(dashboard).get("/api/dashboard/kpis?risk_type=litigation").json()
    assert {item["kpi_code"] for item in body["items"]} == {"KPI-LI-05", "KPI-EO-01", "KPI-RC-01"}


def test_empty_history_keeps_reference_null():
    dashboard, *_ = service([])
    body = client(dashboard).get("/api/dashboard/kpis").json()
    assert body["period_reference"] is None and body["items"] == []


@pytest.mark.parametrize("query", ["period=custom", "period=custom&period_start=2026-02-01&period_end=2026-01-01", "period_start=2026-01-01", "risk_type=unknown", "entity="])
def test_invalid_filter_is_rejected(query):
    dashboard, repo, *_ = service([])
    assert client(dashboard).get("/api/dashboard/kpis?" + query).status_code == 422
    assert repo.reads == 0


@pytest.mark.parametrize("path", ["/api/dashboard/kpis", "/api/dashboard/analysis", "/api/technical/kpis/KPI-CD-03"])
def test_missing_bearer_is_unauthenticated(path):
    dashboard, repo, *_ = service([])
    assert client(dashboard, authenticated=False).get(path).status_code == 401
    assert repo.reads == 0


def test_effective_revocation_denies_before_read_and_audits_once():
    dashboard, repo, audit, _ = service([], Security(denied=True))
    response = client(dashboard).get("/api/dashboard/kpis")
    assert response.status_code == 403
    assert repo.reads == 0 and len(audit.events) == 1
    assert response.json() == {"detail": "Acceso no autorizado"}


def test_technical_kpi_uses_revalidated_role_not_supplied_ti_claim():
    dashboard, repo, audit, _ = service([row("KPI-CD-03")], Security(actor("JURIDICO")))
    with pytest.raises(AuthorizationError):
        dashboard.technical(KpiRecalculationContext(uuid4(), uuid4(), actor=actor("TI")))
    assert repo.reads == 0 and len(audit.events) == 1


def test_technical_available_zero_and_no_data_are_distinct():
    dashboard, *_ = service([row("KPI-CD-03")], Security(actor("TI")))
    assert client(dashboard).get("/api/technical/kpis/KPI-CD-03").json()["value"] == "0"
    body = client(dashboard).get("/api/technical/kpis/KPI-CD-03?period_start=2027-01-01").json()
    assert body["value"] is None and body["availability"] == "NO_DISPONIBLE" and body["calculated_at"] is None


def test_current_month_and_custom_bounds_are_explicit():
    dashboard, *_ = service([row(month=7)])
    body = client(dashboard).get("/api/dashboard/kpis?period=current_month").json()
    assert (body["period_start"], body["period_end"], body["items"]) == ("2026-08-01", "2026-08-31", [])


@pytest.mark.parametrize("period,start", [("last_3_months", date(2025, 11, 1)), ("last_6_months", date(2025, 8, 1)), ("last_12_months", date(2025, 2, 1))])
def test_calendar_month_period_options(period, start):
    assert period_bounds(DashboardFilters(period=period), date(2026, 1, 1), date(2026, 8, 1)) == (start, date(2026, 1, 31))


class AnalysisQuery:
    def __init__(self, values):
        self.values = values
        self.query = None

    def list_for_dashboard(self, query, context, *, reader, latest_only=False):
        self.query = query
        values = self.values if query.analytic_run_id is None else [value for value in self.values if value[0]["id"] == query.analytic_run_id]
        return reader(values, current_run_id=None if not self.values else self.values[0][0]["id"])


def analysis_run(month):
    identity = uuid4()
    run = dict(id=identity, completed_at=datetime(2026, month, 20, tzinfo=UTC), window_start=datetime(2026, month, 1, tzinfo=UTC), window_end=datetime(2026, month, 28, tzinfo=UTC))
    result = ProactiveAnalysisReadResult(ProactiveAnalysisResult("COMPLETED", "a" * 64, identity, uuid4(), executive_summary="Resumen persistido."))
    return run, result, {}


def test_analysis_default_reads_latest_only_and_preserves_summary():
    values = [analysis_run(6), analysis_run(5)]
    query = AnalysisQuery(values)
    dashboard = DashboardService(None, query)
    page = dashboard.analysis(DashboardFilters(), ProactiveQueryContext(uuid4(), uuid4(), actor()))
    assert len(page.items) == 1 and page.current_analytic_run_id == values[0][0]["id"]
    assert page.items[0].executive_summary == "Resumen persistido." and page.alert_count == 0


def test_analysis_explicit_period_selects_persisted_historical_runs():
    values = [analysis_run(6), analysis_run(5)]
    dashboard = DashboardService(None, AnalysisQuery(values))
    page = dashboard.analysis(DashboardFilters(period="custom", period_start=date(2026, 5, 1), period_end=date(2026, 5, 31)), ProactiveQueryContext(uuid4(), uuid4(), actor()), historical=True)
    assert [item.analytic_run_id for item in page.items] == [values[1][0]["id"]]


def test_analysis_transport_accepts_completed_run_identifier():
    values = [analysis_run(6)]
    dashboard = DashboardService(None, AnalysisQuery(values))
    response = client(dashboard).get("/api/dashboard/analysis?analytic_run_id=" + str(values[0][0]["id"]))
    assert response.status_code == 200
    assert response.json()["items"][0]["analytic_run_id"] == str(values[0][0]["id"])


def test_invalid_bearer_is_rejected_by_authentication_boundary():
    dashboard, repo, *_ = service([])
    class InvalidAuthentication:
        def authenticated_principal(self, token):
            raise AuthenticationError("Credenciales no válidas")
    app = FastAPI()
    app.include_router(router)
    app.state.security_service = InvalidAuthentication()
    app.dependency_overrides[dashboard_service_for] = lambda: dashboard
    response = TestClient(app).get("/api/dashboard/kpis", headers={"Authorization": "Bearer invalid-test-token"})
    assert response.status_code == 401 and repo.reads == 0
