"""Contratos internos del núcleo de indicadores."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID

from app.security.models import AuthenticatedPrincipal


KPI_CODES = frozenset({
    "KPI-RC-01", "KPI-RC-03", "KPI-LI-01", "KPI-LI-05",
    "KPI-CN-02", "KPI-CN-03", "KPI-EO-01", "KPI-CD-03",
})


class KpiCalculationError(Exception):
    """El cálculo de indicadores no puede confirmarse."""


class ProactiveAnalysisError(Exception):
    """El análisis proactivo no puede confirmarse de forma segura."""

    def __init__(self, message: str, *, safe_cause_code: str = "PROACTIVE_ANALYSIS_FAILED") -> None:
        super().__init__(message)
        self.safe_cause_code = safe_cause_code


@dataclass(frozen=True)
class KpiRecalculationContext:
    operation_id: UUID
    correlation_id: UUID
    actor: AuthenticatedPrincipal | None = None
    process_identifier: str | None = None

    def __post_init__(self) -> None:
        if self.actor is None and not self.process_identifier:
            raise ValueError("la recalculación requiere actor o proceso identificable")


@dataclass(frozen=True)
class KpiRecalculationRequest:
    kpi_codes: tuple[str, ...]
    period_start: date
    period_end: date
    as_of_date: date


@dataclass(frozen=True)
class KpiObservationResult:
    kpi_code: str
    period_start: date
    period_end: date
    dimensions: Mapping[str, str]
    value: Decimal | None
    availability: str
    as_of_date: date


@dataclass(frozen=True)
class KpiRecalculationResult:
    analytic_run_id: UUID
    observations: tuple[KpiObservationResult, ...]


@dataclass(frozen=True)
class KpiQuery:
    kpi_code: str | None = None
    period_start: date | None = None
    period_end: date | None = None


@dataclass(frozen=True)
class ProactiveAnalysisContext:
    job_run_id: UUID
    operation_id: UUID
    correlation_id: UUID
    process_identifier: str

    def __post_init__(self) -> None:
        if not self.process_identifier:
            raise ValueError("el análisis proactivo requiere un proceso identificable")


@dataclass(frozen=True)
class ProactiveQueryContext:
    operation_id: UUID
    correlation_id: UUID
    actor: AuthenticatedPrincipal


@dataclass(frozen=True)
class ProactiveAnalysisQuery:
    analytic_run_id: UUID | None = None
    period_start: date | None = None
    period_end: date | None = None


@dataclass(frozen=True)
class ProactiveObservation:
    source_observation_id: UUID
    source_analytic_run_id: UUID
    kpi_code: str
    canonical_dimensions_key: Mapping[str, Any]
    period_start: date
    period_end: date
    as_of_date: date
    availability: str
    value: Decimal | None
    calculated_at: datetime


@dataclass(frozen=True)
class ProactiveEvaluationResult:
    id: UUID
    analytic_run_id: UUID
    kpi_code: str
    canonical_dimensions_key: Mapping[str, Any]
    evaluation_period: date
    outcome: str
    signal_detected: bool
    trend_direction: str | None
    recurrence_month_count: int | None
    current_value: Decimal | None
    reference_value: Decimal | None
    absolute_variation: Decimal | None
    percentage_variation: Decimal | None
    rules_applied: tuple[str, ...]
    not_evaluated_reason: str | None


@dataclass(frozen=True)
class ProactiveFindingResult:
    id: UUID
    analytic_run_id: UUID
    proactive_evaluation_id: UUID
    kpi_code: str
    dimensions: Mapping[str, Any]
    period_start: date
    period_end: date
    current_value: Decimal
    reference_value: Decimal
    variation: Decimal
    triggered_rule: str
    recurrent_pattern: str | None
    description: str


@dataclass(frozen=True)
class ProactiveContextReferenceResult:
    id: UUID
    finding_id: UUID
    kpi_code: str
    dimensions: Mapping[str, Any]
    period_start: date
    period_end: date
    context_identifiers: Mapping[str, Any]
    operation_id: UUID
    correlation_id: UUID


@dataclass(frozen=True)
class ProactiveAnalysisResult:
    status: str
    input_fingerprint_sha256: str
    analytic_run_id: UUID | None
    run_operation_id: UUID | None
    evaluations: tuple[ProactiveEvaluationResult, ...] = ()
    findings: tuple[ProactiveFindingResult, ...] = ()
    executive_summary: str | None = None


@dataclass(frozen=True)
class ProactiveAnalysisReadResult:
    result: ProactiveAnalysisResult
    context_references: tuple[ProactiveContextReferenceResult, ...] = ()
