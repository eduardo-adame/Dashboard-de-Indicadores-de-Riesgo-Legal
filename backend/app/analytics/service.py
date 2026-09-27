"""Servicio reusable para recalcular indicadores sin cableado de pipeline."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable
from uuid import UUID, uuid5

import psycopg

from app.analytics.models import (
    KPI_CODES, KpiCalculationError, KpiObservationResult, KpiQuery,
    KpiRecalculationContext, KpiRecalculationRequest, KpiRecalculationResult,
    ProactiveAnalysisContext, ProactiveAnalysisError, ProactiveAnalysisQuery,
    ProactiveAnalysisReadResult, ProactiveAnalysisResult,
    ProactiveContextReferenceResult, ProactiveEvaluationResult,
    ProactiveFindingResult, ProactiveQueryContext,
)
from app.analytics.proactive_rules import (
    build_finding, canonical_dimensions_json, canonical_fingerprint,
    derive_context_id, derive_context_operation_id, derive_run_id,
    derive_run_operation_id as derive_proactive_run_operation_id,
    evaluate_snapshot, executive_summary, rules_reference, run_metadata,
    select_snapshot_closure, terminal_months_used, valid_fingerprint_contract,
)
from app.projection.models import ProjectionResult
from app.security.models import SecurityError


_RUN_NAMESPACE = UUID("ef79661e-3c23-4be1-8e8e-ff9dc54f5966")
_ROUTING = {
    "CONTRATO": ("KPI-RC-01", "KPI-RC-03"), "LITIGIO": ("KPI-LI-01", "KPI-LI-05"),
    "OBLIGACION": ("KPI-CN-02",), "INCIDENTE": ("KPI-CN-03",), "ASUNTO": ("KPI-EO-01",),
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


class ProactiveAnalysisService:
    """Ejecuta análisis mensual determinista sobre snapshots certificados."""

    def __init__(self, repository, audit_repository=None) -> None:
        if audit_repository is None:
            raise ValueError("la auditoría del análisis proactivo es obligatoria")
        self.repository = repository
        self.audit_repository = audit_repository

    def execute(self, context: ProactiveAnalysisContext) -> ProactiveAnalysisResult:
        for attempt in range(3):
            try:
                return self._execute_once(context)
            except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected) as exc:
                if attempt == 2:
                    raise ProactiveAnalysisError(
                        "la ejecución concurrente no pudo confirmarse",
                        safe_cause_code="CONCURRENT_RUN_NOT_CONFIRMED",
                    ) from exc
        raise AssertionError("unreachable")

    def _execute_once(self, context: ProactiveAnalysisContext) -> ProactiveAnalysisResult:
        durable_run_id: UUID | None = None
        authoritative_job = None
        try:
            frozen = self._freeze(context)
            if isinstance(frozen, ProactiveAnalysisResult):
                return frozen
            durable_run_id, authoritative_job = frozen
            return self._finalize(durable_run_id, authoritative_job)
        except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
            raise
        except ProactiveAnalysisError as exc:
            if durable_run_id is not None and authoritative_job is not None:
                self._record_proactive_failure(durable_run_id, authoritative_job, exc.safe_cause_code)
            raise
        except Exception as exc:
            if durable_run_id is not None and authoritative_job is not None:
                self._record_proactive_failure(durable_run_id, authoritative_job, type(exc).__name__[:80])
            raise ProactiveAnalysisError("el análisis proactivo no pudo confirmarse") from exc

    def _freeze(self, context: ProactiveAnalysisContext):
        with self.repository.transaction() as connection:
            job = self.repository.proactive_job(connection, context.job_run_id)
            self._validate_job(job, context)

            existing = self.repository.proactive_run_for_job(connection, context.job_run_id, for_update=True)
            if existing is not None:
                fingerprint = _fingerprint_from_run(existing)
                if existing["state"] == "COMPLETED":
                    return _proactive_result(self.repository, connection, existing, fingerprint)
                return existing["id"], job

            relevance_input = self.repository.eligible_proactive_observations(connection)
            fingerprint = canonical_fingerprint(relevance_input)
            if not relevance_input:
                return ProactiveAnalysisResult("NO_RELEVANT_WORK", fingerprint, None, None)

            latest = self.repository.latest_completed_proactive_run(connection)
            if latest is not None:
                previous = _fingerprint_from_run(latest)
                if previous == fingerprint:
                    return ProactiveAnalysisResult("NO_RELEVANT_WORK", fingerprint, None, None)

            snapshot = select_snapshot_closure(relevance_input)
            window_start, window_end, codes = run_metadata(snapshot)
            operation_id = derive_proactive_run_operation_id(job["operation_id"], fingerprint)
            run_id = derive_run_id(operation_id)
            metadata = rules_reference(
                fingerprint=fingerprint,
                job_run_id=job["id"],
                parent_operation_id=job["operation_id"],
            )
            self.repository.acquire_run_lock(connection)
            created = self.repository.create_proactive_run(
                connection,
                run_id=run_id,
                operation_id=operation_id,
                correlation_id=job["correlation_id"],
                window_start=window_start,
                window_end=window_end,
                codes=codes,
                rules_reference=metadata,
            )
            if created is None:
                concurrent = self.repository.proactive_run_for_job(connection, job["id"], for_update=True)
                if concurrent is None:
                    raise ProactiveAnalysisError("la ejecución concurrente no pudo confirmarse", safe_cause_code="CONCURRENT_RUN_NOT_CONFIRMED")
                return concurrent["id"], job
            self.repository.persist_proactive_snapshot(
                connection,
                run_id=run_id,
                job_run_id=job["id"],
                observations=snapshot,
            )
            return run_id, job

    def _finalize(self, run_id: UUID, job) -> ProactiveAnalysisResult:
        with self.repository.transaction() as connection:
            run = self.repository.proactive_run_for_job(connection, job["id"], for_update=True)
            if run is None or run["id"] != run_id:
                raise ProactiveAnalysisError("la ejecución persistida no coincide", safe_cause_code="RUN_BINDING_MISMATCH")
            fingerprint = _fingerprint_from_run(run)
            if run["state"] == "COMPLETED":
                return _proactive_result(self.repository, connection, run, fingerprint)
            if run["state"] == "FAILED":
                self.repository.restart_failed_run(connection, run_id)

            snapshot = self.repository.proactive_snapshot(connection, run_id)
            if not snapshot:
                raise ProactiveAnalysisError("el snapshot de la ejecución no existe", safe_cause_code="SNAPSHOT_NOT_FOUND")
            evaluations = evaluate_snapshot(run_id, snapshot)
            findings: list[ProactiveFindingResult] = []
            for evaluation in evaluations:
                self.repository.persist_proactive_evaluation(connection, evaluation)
                period_end = _current_period_end(snapshot, evaluation)
                finding = build_finding(run["operation_id"], evaluation, period_end)
                if finding is None:
                    continue
                self.repository.persist_proactive_finding(connection, finding)
                reference = _context_reference(run, job, snapshot, evaluation, finding)
                self.repository.persist_context_reference(connection, reference)
                findings.append(finding)

            summary = executive_summary(findings)
            self.audit_repository.write_audit_event(
                connection,
                actor=None,
                action="PROACTIVE_ANALYSIS",
                resource_type="ANALYTIC_RUN",
                resource_identifier=str(run_id),
                result="SUCCESS",
                correlation_id=job["correlation_id"],
                process_identifier=job["actor_process"],
            )
            self.repository.complete_proactive_run(connection, run_id, summary)
            return ProactiveAnalysisResult(
                "COMPLETED", fingerprint, run_id, run["operation_id"],
                tuple(evaluations), tuple(findings), summary,
            )

    @staticmethod
    def _validate_job(job, context: ProactiveAnalysisContext) -> None:
        if job is None or job["case_type"] != "PROACTIVE_ANALYSIS":
            raise ProactiveAnalysisError("job proactivo no válido", safe_cause_code="JOB_CONTRACT_MISMATCH")
        if job["operation_id"] != context.operation_id:
            raise ProactiveAnalysisError("operación no coincide con el job", safe_cause_code="JOB_OPERATION_MISMATCH")
        if job["correlation_id"] != context.correlation_id:
            raise ProactiveAnalysisError("correlación no coincide con el job", safe_cause_code="JOB_CORRELATION_MISMATCH")
        if job["actor_process"] != context.process_identifier:
            raise ProactiveAnalysisError("proceso no coincide con el job", safe_cause_code="JOB_PROCESS_MISMATCH")

    def _record_proactive_failure(self, run_id: UUID, job, safe_cause: str) -> None:
        try:
            with self.repository.transaction() as connection:
                run = self.repository.proactive_run_for_job(connection, job["id"], for_update=True)
                if run is None or run["id"] != run_id or run["state"] == "COMPLETED":
                    return
                self.repository.fail_run(connection, run_id, safe_cause)
                self.audit_repository.write_audit_event(
                    connection,
                    actor=None,
                    action="PROACTIVE_ANALYSIS",
                    resource_type="ANALYTIC_RUN",
                    resource_identifier=str(run_id),
                    result="FAILURE",
                    correlation_id=job["correlation_id"],
                    safe_cause_code=safe_cause,
                    process_identifier=job["actor_process"],
                )
        except Exception:
            return


class ProactiveAnalysisQueryService:
    def __init__(self, repository, security_service, audit_repository=None) -> None:
        if audit_repository is None:
            raise ValueError("la auditoría de denegaciones es obligatoria")
        self.repository = repository
        self.security_service = security_service
        self.audit_repository = audit_repository

    def latest_completed(self, context: ProactiveQueryContext) -> ProactiveAnalysisReadResult | None:
        rows = self._authorized_runs(ProactiveAnalysisQuery(), context)
        return None if not rows else rows[0]

    def get_completed(self, analytic_run_id: UUID, context: ProactiveQueryContext) -> ProactiveAnalysisReadResult | None:
        rows = self._authorized_runs(ProactiveAnalysisQuery(analytic_run_id=analytic_run_id), context)
        return None if not rows else rows[0]

    def list_completed(self, query: ProactiveAnalysisQuery, context: ProactiveQueryContext) -> tuple[ProactiveAnalysisReadResult, ...]:
        return self._authorized_runs(query, context)

    def _authorized_runs(self, query: ProactiveAnalysisQuery, context: ProactiveQueryContext) -> tuple[ProactiveAnalysisReadResult, ...]:
        try:
            with self.repository.transaction() as connection:
                self.security_service.revalidate_functional_access(connection, context.actor, "dashboard.read")
                runs = self.repository.list_completed_proactive_runs(
                    connection,
                    run_id=query.analytic_run_id,
                    period_start=query.period_start,
                    period_end=query.period_end,
                )
                return tuple(_proactive_read_result(self.repository, connection, run) for run in runs)
        except SecurityError:
            with self.repository.transaction() as connection:
                self.audit_repository.write_audit_event(
                    connection,
                    actor=context.actor,
                    action="AUTHORIZATION_DENIED",
                    resource_type="PROACTIVE_ANALYSIS",
                    resource_identifier=None,
                    result="DENIED",
                    correlation_id=context.correlation_id,
                    safe_cause_code="DEFAULT_DENY",
                )
            raise


def _fingerprint_from_run(run) -> str:
    reference = run["rules_reference"]
    if not valid_fingerprint_contract(reference):
        raise ProactiveAnalysisError(
            "el último análisis completado no contiene una huella válida",
            safe_cause_code="PROACTIVE_FINGERPRINT_CONTRACT_GAP",
        )
    return reference["input_fingerprint_sha256"]


def _proactive_result(repository, connection, run, fingerprint: str) -> ProactiveAnalysisResult:
    evaluations = tuple(_evaluation_from_row(row) for row in repository.proactive_evaluations(connection, run["id"]))
    findings = tuple(_finding_from_row(row) for row in repository.proactive_findings(connection, run["id"]))
    return ProactiveAnalysisResult(
        "COMPLETED", fingerprint, run["id"], run["operation_id"],
        evaluations, findings, run["executive_summary"],
    )


def _proactive_read_result(repository, connection, run) -> ProactiveAnalysisReadResult:
    result = _proactive_result(repository, connection, run, _fingerprint_from_run(run))
    references = tuple(
        ProactiveContextReferenceResult(
            row["id"], row["finding_id"], row["kpi_code"], row["dimensions"],
            row["period_start"], row["period_end"], row["context_identifiers"],
            row["operation_id"], row["correlation_id"],
        )
        for row in repository.proactive_context_references(connection, run["id"])
    )
    return ProactiveAnalysisReadResult(result, references)


def _evaluation_from_row(row) -> ProactiveEvaluationResult:
    return ProactiveEvaluationResult(
        row["id"], row["analytic_run_id"], row["kpi_code"], row["canonical_dimensions_key"],
        row["evaluation_period"], row["outcome"], row["signal_detected"], row["trend_direction"],
        row["recurrence_month_count"], row["current_value"], row["reference_value"],
        row["absolute_variation"], row["percentage_variation"], tuple(row["rules_applied"]),
        row["not_evaluated_reason"],
    )


def _finding_from_row(row) -> ProactiveFindingResult:
    return ProactiveFindingResult(
        row["id"], row["analytic_run_id"], row["proactive_evaluation_id"], row["kpi_code"],
        row["dimensions"], row["period_start"], row["period_end"], row["current_value"],
        row["reference_value"], row["variation"], row["triggered_rule"],
        row["recurrent_pattern"], row["description"],
    )


def _current_period_end(snapshot, evaluation: ProactiveEvaluationResult) -> date:
    dimensions = canonical_dimensions_json(evaluation.canonical_dimensions_key)
    for row in snapshot:
        if row.kpi_code == evaluation.kpi_code and row.period_start == evaluation.evaluation_period and canonical_dimensions_json(row.canonical_dimensions_key) == dimensions:
            return row.period_end
    raise ProactiveAnalysisError("el periodo actual no existe en el snapshot", safe_cause_code="CURRENT_PERIOD_NOT_FOUND")


def _context_reference(run, job, snapshot, evaluation: ProactiveEvaluationResult, finding: ProactiveFindingResult) -> ProactiveContextReferenceResult:
    dimensions = canonical_dimensions_json(evaluation.canonical_dimensions_key)
    source_ids = sorted(
        str(row.source_observation_id)
        for row in snapshot
        if row.kpi_code == evaluation.kpi_code and canonical_dimensions_json(row.canonical_dimensions_key) == dimensions
    )
    identifiers = {
        "proactive_evaluation_id": str(evaluation.id),
        "source_observation_ids": source_ids,
        "terminal_months_used": list(terminal_months_used(evaluation)),
    }
    return ProactiveContextReferenceResult(
        derive_context_id(finding.id), finding.id, finding.kpi_code, finding.dimensions,
        finding.period_start, finding.period_end, identifiers,
        derive_context_operation_id(run["operation_id"], finding.id), job["correlation_id"],
    )


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
