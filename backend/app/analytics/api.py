"""Fronteras HTTP de lectura; los permisos y resultados pertenecen a los servicios."""
from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
import psycopg

from app.analytics.api_models import AnalysisFilters, AnalysisPage, DashboardFilters, KpiPage, TechnicalKpi
from app.analytics.dashboard_service import DashboardService
from app.analytics.models import KpiRecalculationContext, ProactiveAnalysisError, ProactiveQueryContext
from app.analytics.repository import AnalyticsRepository
from app.analytics.service import KpiQueryService, ProactiveAnalysisQueryService
from app.config import Settings
from app.security.api import current_principal, security_settings, service_for
from app.security.models import AuthenticatedPrincipal, AuditPersistenceError, SecurityError
from app.security.service import SecurityService


router = APIRouter(prefix="/api", tags=["analytics"])


def dashboard_service_for(request: Request, security: SecurityService = Depends(service_for), settings: Settings = Depends(security_settings)) -> DashboardService:
    supplied = getattr(request.app.state, "dashboard_service", None)
    if supplied is not None:
        return supplied
    repository = AnalyticsRepository(settings.psycopg_conninfo)
    return DashboardService(KpiQueryService(repository, security, security.repository), ProactiveAnalysisQueryService(repository, security, security.repository))


def _read(call):
    try:
        return call()
    except AuditPersistenceError:
        raise HTTPException(503, "La lectura no puede confirmarse") from None
    except SecurityError:
        raise HTTPException(403, "Acceso no autorizado") from None
    except (ProactiveAnalysisError, psycopg.Error):
        raise HTTPException(503, "La lectura no puede confirmarse") from None
    except ValueError:
        raise HTTPException(422, "Filtros no válidos") from None


@router.get("/dashboard/kpis", response_model=KpiPage)
def dashboard_kpis(filters: DashboardFilters = Query(), principal: AuthenticatedPrincipal = Depends(current_principal), service: DashboardService = Depends(dashboard_service_for)):
    context = KpiRecalculationContext(uuid4(), uuid4(), actor=principal)
    return _read(lambda: service.kpis(filters, context))


@router.get("/technical/kpis/KPI-CD-03", response_model=TechnicalKpi)
def technical_kpi(period_start: date | None = None, period_end: date | None = None, principal: AuthenticatedPrincipal = Depends(current_principal), service: DashboardService = Depends(dashboard_service_for)):
    context = KpiRecalculationContext(uuid4(), uuid4(), actor=principal)
    return _read(lambda: service.technical(context, period_start=period_start, period_end=period_end))


@router.get("/dashboard/analysis", response_model=AnalysisPage)
def dashboard_analysis(request: Request, filters: AnalysisFilters = Query(), principal: AuthenticatedPrincipal = Depends(current_principal), service: DashboardService = Depends(dashboard_service_for)):
    context = ProactiveQueryContext(uuid4(), uuid4(), principal)
    return _read(lambda: service.analysis(filters, context, analytic_run_id=filters.analytic_run_id, historical="period" in request.query_params))
