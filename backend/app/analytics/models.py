"""Contratos internos del núcleo de indicadores."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping
from uuid import UUID

from app.security.models import AuthenticatedPrincipal


KPI_CODES = frozenset({
    "KPI-RC-01", "KPI-RC-03", "KPI-LI-01", "KPI-LI-05",
    "KPI-CN-02", "KPI-CN-03", "KPI-EO-01", "KPI-CD-03",
})


class KpiCalculationError(Exception):
    """El cálculo de indicadores no puede confirmarse."""


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
