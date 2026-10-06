"""Presentación autorizada de resultados persistidos, sin nuevos cálculos."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

from app.analytics.api_models import AnalysisItem, AnalysisPage, ContextReferenceItem, DashboardFilters, EvaluationItem, FindingItem, KpiItem, KpiPage, TechnicalKpi
from app.analytics.models import ProactiveAnalysisQuery


CATALOG = {
    "KPI-RC-01": ("Tiempo de ciclo del contrato", "MVP-COMPLEMENTARIO", "días"),
    "KPI-RC-03": ("Contratos próximos a vencimiento sin revisión", "MVP-NÚCLEO", "contratos"),
    "KPI-LI-01": ("Exposición total por litigios activos", "MVP-NÚCLEO", "importe"),
    "KPI-LI-05": ("Nuevos litigios por periodo", "MVP-NÚCLEO", "litigios"),
    "KPI-CN-02": ("Obligaciones regulatorias vencidas sin atender", "MVP-NÚCLEO", "obligaciones"),
    "KPI-CN-03": ("Incidentes de incumplimiento por periodo", "MVP-NÚCLEO", "incidentes"),
    "KPI-EO-01": ("Volumen de asuntos jurídicos gestionados", "MVP-COMPLEMENTARIO", "asuntos"),
}
RISK_PREFIX = {"contractual": "KPI-RC-", "litigation": "KPI-LI-", "compliance": "KPI-CN-"}


def _month(value, offset=0):
    ordinal = value.year * 12 + value.month - 1 + offset
    return date(ordinal // 12, ordinal % 12 + 1, 1)


def period_bounds(filters, reference, today):
    if filters.period == "custom":
        return filters.period_start, filters.period_end
    if filters.period == "current_month":
        start = _month(today)
        return start, _month(start, 1) - timedelta(days=1)
    if reference is None:
        return None, None
    months = {"last_3_months": 3, "last_6_months": 6, "last_12_months": 12}[filters.period]
    return _month(reference, 1 - months), _month(reference, 1) - timedelta(days=1)


def _matches(code, dimensions, filters):
    if code not in CATALOG:
        return False
    # Los complementarios siguen siendo contexto, no reciben una categoría de riesgo nueva.
    prefix = RISK_PREFIX.get(filters.risk_type)
    if prefix and code not in {"KPI-RC-01", "KPI-EO-01"} and not code.startswith(prefix):
        return False
    return filters.entity is None or "entity" not in dimensions or dimensions["entity"] == filters.entity


def _decimal(value):
    return None if value is None else str(value)


def _dto_values(item):
    values = asdict(item)
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}


class DashboardService:
    def __init__(self, kpi_query, analysis_query, *, today=date.today):
        self.kpi_query = kpi_query
        self.analysis_query = analysis_query
        self.today = today

    def kpis(self, filters: DashboardFilters, context):
        return self.kpi_query.list_for_dashboard(context, reader=lambda rows: self._kpi_page(rows, filters))

    def _kpi_page(self, rows, filters):
        today = self.today()
        executive = [row for row in rows if row["kpi_code"] in CATALOG]
        reference = max((row["period_start"] for row in executive if row["availability"] == "DISPONIBLE" and row["value"] is not None and row["period_end"] < today), default=None)
        start, end = period_bounds(filters, reference, today)
        selected = [row for row in executive if start is not None and end is not None and row["period_start"] >= start and row["period_end"] <= end and _matches(row["kpi_code"], row["dimensions"], filters)]
        valid_rc03 = [row for row in selected if row["kpi_code"] == "KPI-RC-03" and row["availability"] == "DISPONIBLE" and row["value"] is not None]
        latest_rc03 = max(valid_rc03, key=lambda row: (row["calculated_at"], str(row["id"])), default=None)
        selected = [row for row in selected if row["kpi_code"] != "KPI-RC-03"] + ([] if latest_rc03 is None else [latest_rc03])
        selected.sort(key=lambda row: (row["kpi_code"], row["period_start"], str(row["dimensions"]), str(row["id"])))
        items = []
        for row in selected:
            name, classification, unit = CATALOG[row["kpi_code"]]
            items.append(KpiItem(**{key: row[key] for key in ("kpi_code", "period_start", "period_end", "as_of_date", "calculated_at", "dimensions", "availability")}, name=name, classification=classification, unit=unit, value=_decimal(row["value"]), entity_filter_applicable="entity" in row["dimensions"]))
        return KpiPage(period_start=start, period_end=end, period_reference=reference, filters=filters, items=items)

    def technical(self, context, *, period_start=None, period_end=None):
        if period_start and period_end and period_start > period_end:
            raise ValueError("El rango requiere fechas ordenadas")
        def read(rows):
            rows = [row for row in rows if (period_start is None or row["period_start"] >= period_start) and (period_end is None or row["period_end"] <= period_end)]
            row = max(rows, key=lambda item: (item["calculated_at"], str(item["id"])), default=None)
            if row is None:
                return TechnicalKpi(value=None, availability="NO_DISPONIBLE", as_of_date=None, calculated_at=None, period_start=None, period_end=None)
            return TechnicalKpi(value=_decimal(row["value"]), **{key: row[key] for key in ("availability", "as_of_date", "calculated_at", "period_start", "period_end")})
        return self.kpi_query.list_for_dashboard(context, technical=True, reader=read)

    def analysis(self, filters, context, *, analytic_run_id=None, historical=False):
        query = ProactiveAnalysisQuery(analytic_run_id=analytic_run_id)
        def read(values, *, current_run_id):
            if not values:
                return AnalysisPage(current_analytic_run_id=current_run_id, items=[], alert_count=0)
            reference = values[0][0]["window_end"].date()
            start, end = period_bounds(filters, reference, self.today())
            if not historical and analytic_run_id is None:
                values = values[:1]
                start, end = None, None
            items = []
            for run, read_result, dates in values:
                if historical and (start is None or run["window_end"].date() < start or run["window_start"].date() > end):
                    continue
                def allowed(code, dimensions, first, last):
                    return _matches(code, dimensions, filters) and (start is None or last >= start) and (end is None or first <= end)
                evaluations = [EvaluationItem(**_dto_values(item), entity_filter_applicable="entity" in item.canonical_dimensions_key) for item in read_result.result.evaluations if allowed(item.kpi_code, item.canonical_dimensions_key, item.evaluation_period, _month(item.evaluation_period, 1) - timedelta(days=1))]
                findings = [FindingItem(**_dto_values(item), created_at=dates[item.id], entity_filter_applicable="entity" in item.dimensions) for item in read_result.result.findings if allowed(item.kpi_code, item.dimensions, item.period_start, item.period_end)]
                finding_ids = {item.id for item in findings}
                references = [ContextReferenceItem(**asdict(item)) for item in read_result.context_references if item.finding_id in finding_ids]
                items.append(AnalysisItem(analytic_run_id=run["id"], completed_at=run["completed_at"], window_start=run["window_start"], window_end=run["window_end"], executive_summary=read_result.result.executive_summary, evaluations=evaluations, findings=findings, context_references=references))
            return AnalysisPage(current_analytic_run_id=current_run_id, items=items, alert_count=sum(len(item.findings) for item in items))
        return self.analysis_query.list_for_dashboard(query, context, reader=read, latest_only=not historical and analytic_run_id is None)
