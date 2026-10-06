"""Contratos de transporte de las lecturas ejecutivas y técnicas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TransportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DashboardFilters(TransportModel):
    period: Literal["last_3_months", "last_6_months", "last_12_months", "current_month", "custom"] = "last_6_months"
    period_start: date | None = None
    period_end: date | None = None
    risk_type: Literal["all", "contractual", "litigation", "compliance"] = "all"
    entity: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def valid_period(self):
        if self.period == "custom":
            if self.period_start is None or self.period_end is None or self.period_start > self.period_end:
                raise ValueError("El rango personalizado requiere fechas ordenadas")
        elif self.period_start is not None or self.period_end is not None:
            raise ValueError("Las fechas explícitas requieren periodo personalizado")
        return self


class KpiItem(TransportModel):
    kpi_code: str
    name: str
    classification: str
    unit: str
    period_start: date
    period_end: date
    as_of_date: date
    calculated_at: datetime
    dimensions: dict[str, Any]
    value: str | None
    availability: Literal["DISPONIBLE", "NO_DISPONIBLE"]
    entity_filter_applicable: bool


class AnalysisFilters(DashboardFilters):
    analytic_run_id: UUID | None = None


class KpiPage(TransportModel):
    period_start: date | None
    period_end: date | None
    period_reference: date | None
    filters: DashboardFilters
    items: list[KpiItem]


class TechnicalKpi(TransportModel):
    kpi_code: Literal["KPI-CD-03"] = "KPI-CD-03"
    value: str | None
    availability: Literal["DISPONIBLE", "NO_DISPONIBLE"]
    as_of_date: date | None
    calculated_at: datetime | None
    period_start: date | None
    period_end: date | None


class EvaluationItem(TransportModel):
    id: UUID
    analytic_run_id: UUID
    kpi_code: str
    canonical_dimensions_key: dict[str, Any]
    evaluation_period: date
    outcome: str
    signal_detected: bool
    trend_direction: str | None
    recurrence_month_count: int | None
    current_value: str | None
    reference_value: str | None
    absolute_variation: str | None
    percentage_variation: str | None
    rules_applied: list[str]
    not_evaluated_reason: str | None
    entity_filter_applicable: bool


class FindingItem(TransportModel):
    id: UUID
    analytic_run_id: UUID
    proactive_evaluation_id: UUID
    kpi_code: str
    dimensions: dict[str, Any]
    period_start: date
    period_end: date
    current_value: str
    reference_value: str
    variation: str
    triggered_rule: str
    recurrent_pattern: str | None
    description: str
    created_at: datetime
    entity_filter_applicable: bool


class ContextReferenceItem(TransportModel):
    id: UUID
    finding_id: UUID
    kpi_code: str
    dimensions: dict[str, Any]
    period_start: date
    period_end: date
    context_identifiers: dict[str, Any]
    operation_id: UUID
    correlation_id: UUID


class AnalysisItem(TransportModel):
    analytic_run_id: UUID
    completed_at: datetime
    window_start: datetime
    window_end: datetime
    executive_summary: str
    evaluations: list[EvaluationItem]
    findings: list[FindingItem]
    context_references: list[ContextReferenceItem]


class AnalysisPage(TransportModel):
    current_analytic_run_id: UUID | None
    items: list[AnalysisItem]
    alert_count: int
