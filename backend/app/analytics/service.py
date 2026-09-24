"""Servicio reusable para recalcular indicadores sin cableado de pipeline."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable
from uuid import UUID, uuid5

from app.analytics.models import (
    KPI_CODES, KpiCalculationError, KpiObservationResult, KpiQuery,
    KpiRecalculationContext, KpiRecalculationRequest, KpiRecalculationResult,
)
from app.projection.models import ProjectionResult
from app.security.models import SecurityError


_RUN_NAMESPACE = UUID("ef79661e-3c23-4be1-8e8e-ff9dc54f5966")
_ROUTING = {
    "CONTRATO": ("KPI-RC-01", "KPI-RC-03"), "LITIGIO": ("KPI-LI-01", "KPI-LI-05"),
    "OBLIGACION": ("KPI-CN-02", "KPI-CN-03"), "INCIDENTE": ("KPI-CN-03",), "ASUNTO": ("KPI-EO-01",),
}


def derive_run_operation_id(parent_operation_id: UUID, request: KpiRecalculationRequest) -> UUID:
    codes = ",".join(sorted(set(request.kpi_codes)))
    return uuid5(_RUN_NAMESPACE, f"{parent_operation_id}:{codes}:{request.period_start.isoformat()}:{request.period_end.isoformat()}:{request.as_of_date.isoformat()}")


def requested_kpis_for_projection(results: Iterable[ProjectionResult]) -> tuple[str, ...]:
    codes = {code for item in results if item.recalculation_required for code in _ROUTING.get(item.entity_type, ())}
    return tuple(sorted(codes))


class KPIRecalculationService:
    def __init__(self, repository, audit_repository=None, security_service=None) -> None:
        if audit_repository is None:
            raise ValueError("la auditoría de recalculación es obligatoria")
        self.repository = repository
        self.audit_repository = audit_repository
        self.security_service = security_service

    def request_for_projection(self, results: Iterable[ProjectionResult], *, period_start: date, period_end: date, as_of_date: date) -> KpiRecalculationRequest | None:
        codes = requested_kpis_for_projection(results)
        return None if not codes else KpiRecalculationRequest(codes, period_start, period_end, as_of_date)

    @staticmethod
    def request_for_final_ocr(*, period_start: date, period_end: date, as_of_date: date) -> KpiRecalculationRequest:
        return KpiRecalculationRequest(("KPI-CD-03",), period_start, period_end, as_of_date)

    def recalculate(self, request: KpiRecalculationRequest, context: KpiRecalculationContext) -> KpiRecalculationResult:
        _validate_request(request)
        operation_id = derive_run_operation_id(context.operation_id, request)
        try:
            with self.repository.transaction() as connection:
                self.repository.acquire_run_lock(connection)
                existing = self.repository.run_for_operation(connection, operation_id)
                if existing is not None and existing["state"] == "COMPLETED":
                    return KpiRecalculationResult(existing["id"], _to_observations(self.repository.current_observations(connection, codes=tuple(sorted(set(request.kpi_codes))), period_start=request.period_start, period_end=request.period_end)))
                if existing is None:
                    run_id = self.repository.create_run(connection, operation_id=operation_id, correlation_id=context.correlation_id, codes=tuple(sorted(set(request.kpi_codes))), period_start=request.period_start, period_end=request.period_end, as_of_date=request.as_of_date, parent_operation_id=context.operation_id)
                    if run_id is None:
                        existing = self.repository.run_for_operation(connection, operation_id)
                        if existing is None:
                            raise KpiCalculationError("la ejecución concurrente no pudo confirmarse")
                        return KpiRecalculationResult(existing["id"], _to_observations(self.repository.current_observations(connection, codes=tuple(sorted(set(request.kpi_codes))), period_start=request.period_start, period_end=request.period_end)))
                else:
                    run_id = existing["id"]
                    self.repository.restart_failed_run(connection, run_id)
                observations = self._calculate(connection, request)
                for observation in observations:
                    self.repository.upsert_observation(connection, observation, run_id)
                self._audit(connection, context, "SUCCESS")
                self.repository.complete_run(connection, run_id)
                return KpiRecalculationResult(run_id, tuple(observations))
        except Exception as exc:
            self._record_failure(operation_id, request, context, exc)
            if isinstance(exc, KpiCalculationError):
                raise
            raise KpiCalculationError("el cálculo no pudo confirmarse") from exc

    def _record_failure(self, operation_id: UUID, request: KpiRecalculationRequest, context: KpiRecalculationContext, cause: Exception) -> None:
        try:
            with self.repository.transaction() as connection:
                self.repository.acquire_run_lock(connection)
                run = self.repository.run_for_operation(connection, operation_id)
                if run is None:
                    run_id = self.repository.create_run(connection, operation_id=operation_id, correlation_id=context.correlation_id, codes=tuple(sorted(set(request.kpi_codes))), period_start=request.period_start, period_end=request.period_end, as_of_date=request.as_of_date, parent_operation_id=context.operation_id)
                    if run_id is None:
                        return
                else:
                    run_id = run["id"]
                    if run["state"] == "COMPLETED":
                        return
                self.repository.fail_run(connection, run_id, type(cause).__name__[:80])
                self._audit(connection, context, "FAILURE", type(cause).__name__[:80])
        except Exception:
            return

    def _audit(self, connection, context: KpiRecalculationContext, result: str, safe_cause_code: str | None = None) -> None:
        if self.audit_repository is not None:
            self.audit_repository.write_audit_event(connection, actor=context.actor, action="KPI_RECALCULATION", resource_type="KPI", resource_identifier=None, result=result, correlation_id=context.correlation_id, safe_cause_code=safe_cause_code, process_identifier=context.process_identifier)

    def _calculate(self, connection, request: KpiRecalculationRequest) -> list[KpiObservationResult]:
        output: list[KpiObservationResult] = []
        for code in tuple(sorted(set(request.kpi_codes))):
            output.extend(_CALCULATORS[code](self.repository, connection, request))
        return output


class KpiQueryService:
    def __init__(self, repository, security_service, audit_repository=None) -> None:
        if audit_repository is None:
            raise ValueError("la auditoría de consulta es obligatoria")
        self.repository, self.security_service, self.audit_repository = repository, security_service, audit_repository

    def list(self, query: KpiQuery, context: KpiRecalculationContext):
        try:
            with self.repository.transaction() as connection:
                self.security_service.revalidate_functional_access(connection, context.actor, "kpi.read")
                return self.repository.list_observations(connection, kpi_code=query.kpi_code, period_start=query.period_start, period_end=query.period_end)
        except SecurityError:
            with self.repository.transaction() as connection:
                self.audit_repository.write_audit_event(connection, actor=context.actor, action="AUTHORIZATION_DENIED", resource_type="KPI", resource_identifier=None, result="DENIED", correlation_id=context.correlation_id, safe_cause_code="DEFAULT_DENY")
            raise


def _available(code: str, request: KpiRecalculationRequest, value: Decimal, dimensions: dict[str, str] | None = None) -> KpiObservationResult:
    return KpiObservationResult(code, request.period_start, request.period_end, dimensions or {}, value, "DISPONIBLE", request.as_of_date)

def _unavailable(code: str, request: KpiRecalculationRequest) -> KpiObservationResult:
    return KpiObservationResult(code, request.period_start, request.period_end, {}, None, "NO_DISPONIBLE", request.as_of_date)

def _rc01(repo, connection, request):
    values = []
    for row in repo.contract_rows(connection, request.period_start, request.period_end):
        if row["fecha_firma"] is not None and row["fecha_solicitud"] is not None:
            values.append(Decimal((row["fecha_firma"] - row["fecha_solicitud"]).days))
    return [_unavailable("KPI-RC-01", request)] if not values else [_available("KPI-RC-01", request, sum(values) / Decimal(len(values)))]

def _rc03(repo, connection, request):
    count = sum(1 for row in repo.contracts_snapshot(connection) if row["estado_revision"] == "No iniciado" and request.as_of_date <= row["fecha_vencimiento"] <= request.as_of_date + timedelta(days=60))
    return [_available("KPI-RC-03", request, Decimal(count))]

def _li01(repo, connection, request):
    totals = {severity: Decimal(0) for severity in ("alto", "medio", "bajo")}
    for row in repo.active_litigations(connection):
        severity = row["nivel_severidad"]
        if severity not in totals: continue
        amount = row["estimacion_interna"] if row["estimacion_interna"] is not None else row["monto_reclamado"]
        if amount is not None: totals[severity] += Decimal(amount)
    return [_available("KPI-LI-01", request, totals[item], {"nivel_severidad": item}) for item in ("alto", "medio", "bajo")]

def _li05(repo, connection, request):
    return [_available("KPI-LI-05", request, Decimal(len(repo.litigation_rows(connection, request.period_start, request.period_end))))]

def _cn02(repo, connection, request):
    count = sum(1 for row in repo.compliance_snapshot(connection) if row["fecha_limite"] < request.as_of_date and row["evidencia_cumplimiento"] is None)
    return [_available("KPI-CN-02", request, Decimal(count))]

def _cn03(repo, connection, request):
    counts = defaultdict(int)
    for row in repo.incident_rows(connection, request.period_start, request.period_end): counts[(row["area"], row["nivel_severidad"])] += 1
    known = {(row["area"], row["nivel_severidad"]) for row in repo.incident_dimensions(connection)}
    return [_available("KPI-CN-03", request, Decimal(counts[key]), {"area": key[0], "nivel_severidad": key[1]}) for key in sorted(known)]

def _eo01(repo, connection, request):
    identifiers = defaultdict(set)
    for row in repo.matter_rows(connection, request.period_start, request.period_end): identifiers[(row["tipo_asunto"], row["estado"])].add(row["id_asunto"])
    known = {(row["tipo_asunto"], row["estado"]) for row in repo.matter_dimensions(connection)}
    return [_available("KPI-EO-01", request, Decimal(len(identifiers[key])), {"tipo_asunto": key[0], "estado": key[1]}) for key in sorted(known)]

def _cd03(repo, connection, request):
    rows = repo.ocr_final_rows(connection)
    if not rows: return [_unavailable("KPI-CD-03", request)]
    successful = sum(1 for row in rows if row["confianza_agregada"] is not None and Decimal(row["confianza_agregada"]) >= Decimal("0.80"))
    return [_available("KPI-CD-03", request, Decimal(successful) * Decimal(100) / Decimal(len(rows)))]

_CALCULATORS = {"KPI-RC-01": _rc01, "KPI-RC-03": _rc03, "KPI-LI-01": _li01, "KPI-LI-05": _li05, "KPI-CN-02": _cn02, "KPI-CN-03": _cn03, "KPI-EO-01": _eo01, "KPI-CD-03": _cd03}

def _validate_request(request: KpiRecalculationRequest) -> None:
    expected_end = (request.period_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    if not request.kpi_codes or not set(request.kpi_codes).issubset(KPI_CODES) or request.period_end != expected_end or not request.period_start.day == 1 or not request.period_start <= request.as_of_date <= request.period_end:
        raise KpiCalculationError("solicitud de indicadores no válida")

def _to_observations(rows) -> tuple[KpiObservationResult, ...]:
    return tuple(KpiObservationResult(row["kpi_code"], row["period_start"], row["period_end"], row["dimensions"], row["value"], row["availability"], row["as_of_date"]) for row in rows)
