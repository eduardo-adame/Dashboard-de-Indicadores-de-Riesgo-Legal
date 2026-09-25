"""Orquestación durable entre confirmaciones de dominio y Analytics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from typing import Iterable
from uuid import UUID, uuid5

from app.analytics.models import KpiRecalculationContext, KpiRecalculationRequest
from app.analytics.repository import AnalyticsRepository
from app.analytics.service import KPIRecalculationService, requested_kpis_for_projection
from app.projection.models import ProjectionContext, ProjectionResult
from app.projection.repository import ProjectionRepository
from app.projection.service import ProjectionService
from app.validation.models import QuarantineState
from app.validation.service import ReinjectionValidationResult, ValidationContext, ValidationService


_INTEGRATION_NAMESPACE = UUID("5c900013-4ee2-43c4-938f-20d729bfe73b")
_PROCESS_IDENTIFIER = "kpi-integration"
_FINAL_OCR_STATES = frozenset({"Exitoso", "Rechazado por baja confianza"})
_SNAPSHOT_CODES = frozenset({"KPI-RC-03", "KPI-LI-01", "KPI-CN-02"})


class KpiIntegrationError(RuntimeError):
    """La solicitud durable de KPI no pudo confirmarse."""


@dataclass(frozen=True)
class KpiJobOutcome:
    job_id: UUID
    operation_id: UUID
    state: str
    quarantine_state: str | None = None


class KpiIntegrationService:
    def __init__(self, *, conninfo: str, security, repository) -> None:
        self.repository = repository
        self.security = security
        self.projection = ProjectionService(ProjectionRepository(conninfo))
        self.analytics = KPIRecalculationService(AnalyticsRepository(conninfo), security.repository)

    def project_records(self, *, records: Iterable, operation_id: UUID, correlation_id: UUID, actor) -> KpiJobOutcome:
        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, operation_id)
            existing = self.repository.job_for_operation(connection, operation_id, for_update=True)
            if existing is not None:
                outcome = _outcome(existing)
            else:
                results: list[ProjectionResult] = []
                context = ProjectionContext(operation_id, correlation_id, actor)
                for record in records:
                    results.extend(self.projection.project_in_transaction(connection, record, context))
                outcome = self._materialize_projection_job(
                    connection,
                    operation_id=operation_id,
                    correlation_id=correlation_id,
                    results=results,
                    trigger="PROJECTION",
                )
        return self._execute_if_needed(outcome)

    def resume(self, *, operation_id: UUID) -> KpiJobOutcome | None:
        """Reanuda únicamente una solicitud durable ya confirmada.

        Esta comprobación permite que un reintento del despacho no repita los
        efectos de Validation, Documents u OCR cuando la respuesta anterior se
        perdió después del commit.
        """
        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, operation_id)
            existing = self.repository.job_for_operation(connection, operation_id, for_update=True)
            if existing is None:
                return None
            outcome = _outcome(existing)
        return self._execute_if_needed(outcome)

    def reinject(self, *, item_id: UUID, corrected_payload: dict[str, object], correlation_id: UUID, actor, validation: ValidationService) -> KpiJobOutcome:
        operation_id = derive_reinjection_operation_id(item_id, corrected_payload)
        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, operation_id)
            existing = self.repository.job_for_operation(connection, operation_id, for_update=True)
            if existing is not None:
                outcome = _outcome(existing)
            else:
                reinjected = validation.reinject_with_validated_record_in_transaction(
                    connection,
                    item_id=item_id,
                    corrected_payload=corrected_payload,
                    context=ValidationContext(operation_id, correlation_id, actor),
                )
                outcome = self._materialize_reinjection_job(
                    connection,
                    operation_id=operation_id,
                    correlation_id=correlation_id,
                    reinjected=reinjected,
                    actor=actor,
                )
        return self._execute_if_needed(outcome)

    def process_final_ocr(self, *, operation_id: UUID, correlation_id: UUID) -> KpiJobOutcome:
        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, operation_id)
            existing = self.repository.job_for_operation(connection, operation_id, for_update=True)
            if existing is not None:
                outcome = _outcome(existing)
            else:
                row = self.repository.final_ocr_for_operation(connection, operation_id)
                if row is None or not row["is_final"] or row["estado_ocr"] not in _FINAL_OCR_STATES:
                    outcome = self._create_job(connection, operation_id, correlation_id, (), "OCR")
                else:
                    reference = _as_utc_date(row["processed_at"])
                    request = self.analytics.request_for_final_ocr(
                        period_start=_month_start(reference),
                        period_end=_month_end(reference),
                        as_of_date=reference,
                    )
                    outcome = self._create_job(connection, operation_id, correlation_id, (request,), "OCR")
        return self._execute_if_needed(outcome)

    def schedule(self, *, operation_id: UUID, correlation_id: UUID, reference: date) -> KpiJobOutcome:
        from app.analytics.models import KPI_CODES

        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, operation_id)
            existing = self.repository.job_for_operation(connection, operation_id, for_update=True)
            if existing is not None:
                outcome = _outcome(existing)
            else:
                request = KpiRecalculationRequest(
                    tuple(sorted(KPI_CODES)), _month_start(reference), _month_end(reference), reference
                )
                outcome = self._create_job(connection, operation_id, correlation_id, (request,), "SCHEDULED")
        return self._execute_if_needed(outcome)

    def _materialize_reinjection_job(self, connection, *, operation_id: UUID, correlation_id: UUID, reinjected: ReinjectionValidationResult, actor) -> KpiJobOutcome:
        if reinjected.quarantine_item.state != QuarantineState.REINYECTADO or reinjected.validated_record is None:
            return self._create_job(
                connection, operation_id, correlation_id, (), "REINJECTION",
                quarantine_state=reinjected.quarantine_item.state.value,
            )
        results = self.projection.project_in_transaction(
            connection,
            reinjected.validated_record,
            ProjectionContext(operation_id, correlation_id, actor),
        )
        return self._materialize_projection_job(
            connection,
            operation_id=operation_id,
            correlation_id=correlation_id,
            results=results,
            trigger="REINJECTION",
            quarantine_state=reinjected.quarantine_item.state.value,
        )

    def _materialize_projection_job(self, connection, *, operation_id: UUID, correlation_id: UUID, results: Iterable[ProjectionResult], trigger: str, quarantine_state: str | None = None) -> KpiJobOutcome:
        requests = canonical_requests_for_projection(connection, results)
        return self._create_job(
            connection, operation_id, correlation_id, requests, trigger,
            quarantine_state=quarantine_state,
        )

    def _create_job(self, connection, operation_id: UUID, correlation_id: UUID, requests: tuple[KpiRecalculationRequest, ...], trigger: str, quarantine_state: str | None = None) -> KpiJobOutcome:
        refs = {
            "trigger": trigger,
            "requests": [_request_ref(request) for request in requests],
            "analytic_run_ids": [],
        }
        if quarantine_state is not None:
            refs["quarantine_state"] = quarantine_state
        state = "STARTED" if requests else "NO_RELEVANT_WORK"
        starts = [request.period_start for request in requests]
        ends = [request.period_end for request in requests]
        job_id = self.repository.create_job(
            connection,
            operation_id=operation_id,
            correlation_id=correlation_id,
            actor_process=_PROCESS_IDENTIFIER,
            window_start=min(starts) if starts else None,
            window_end=max(ends) if ends else None,
            references=refs,
            state=state,
        )
        return KpiJobOutcome(job_id, operation_id, state, quarantine_state)

    def _execute_if_needed(self, outcome: KpiJobOutcome) -> KpiJobOutcome:
        if outcome.state in {"COMPLETED", "NO_RELEVANT_WORK"}:
            return outcome
        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, outcome.operation_id)
            job = self.repository.job_for_operation(connection, outcome.operation_id, for_update=True)
            if job is None:
                raise KpiIntegrationError("solicitud durable inexistente")
            if job["state"] in {"COMPLETED", "NO_RELEVANT_WORK"}:
                return _outcome(job)
            references = dict(job["result_references"])
            correlation_id = job["correlation_id"]
        try:
            runs = []
            for request in _requests_from_refs(references):
                result = self.analytics.recalculate(
                    request,
                    KpiRecalculationContext(
                        operation_id=outcome.operation_id,
                        correlation_id=correlation_id,
                        process_identifier=_PROCESS_IDENTIFIER,
                    ),
                )
                runs.append(str(result.analytic_run_id))
        except Exception as exc:
            with self.repository.transaction() as connection:
                self.repository.lock_operation(connection, outcome.operation_id)
                job = self.repository.job_for_operation(connection, outcome.operation_id, for_update=True)
                if job is not None:
                    refs = dict(job["result_references"])
                    self.repository.update_job(connection, job["id"], state="FAILED", references=refs, safe_error_code=type(exc).__name__[:80])
            raise KpiIntegrationError("el recálculo no pudo confirmarse") from exc
        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, outcome.operation_id)
            job = self.repository.job_for_operation(connection, outcome.operation_id, for_update=True)
            if job is None:
                raise KpiIntegrationError("solicitud durable inexistente")
            refs = dict(job["result_references"])
            refs["analytic_run_ids"] = runs
            self.repository.update_job(connection, job["id"], state="COMPLETED", references=refs)
            return KpiJobOutcome(job["id"], outcome.operation_id, "COMPLETED", refs.get("quarantine_state"))


def derive_reinjection_operation_id(item_id: UUID, corrected_payload: dict[str, object]) -> UUID:
    encoded = json.dumps(corrected_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return uuid5(_INTEGRATION_NAMESPACE, f"reinjection:{item_id}:{hashlib.sha256(encoded.encode()).hexdigest()}")


def derive_scheduled_operation_id(*, dag_id: str, interval_start: datetime, interval_end: datetime) -> UUID:
    return uuid5(_INTEGRATION_NAMESPACE, f"scheduled:{dag_id}:{interval_start.isoformat()}:{interval_end.isoformat()}")


def canonical_requests_for_projection(connection, results: Iterable[ProjectionResult]) -> tuple[KpiRecalculationRequest, ...]:
    from app.coordination.kpi_repository import KpiIntegrationRepository

    repository = KpiIntegrationRepository
    grouped: dict[tuple[date, date, date], set[str]] = {}
    for result in results:
        if not result.recalculation_required:
            continue
        codes = requested_kpis_for_projection((result,))
        application = repository.projection_reference(
            connection,
            source_record_id=result.source_record_id,
            entity_type=result.entity_type,
            business_id=result.business_id,
        )
        if application is None:
            raise KpiIntegrationError("aplicación analítica no encontrada")
        snapshot_reference = _as_utc_date(application["applied_at"])
        for code in codes:
            reference = snapshot_reference if code in _SNAPSHOT_CODES else repository.business_temporal_value(
                connection, entity_type=result.entity_type, business_id=result.business_id
            )
            if reference is None:
                if code == "KPI-RC-01":
                    continue
                raise KpiIntegrationError("referencia temporal analítica inexistente")
            if isinstance(reference, datetime):
                reference = _as_utc_date(reference)
            key = (_month_start(reference), _month_end(reference), reference)
            grouped.setdefault(key, set()).add(code)
    return tuple(
        KpiRecalculationRequest(tuple(sorted(codes)), start, end, as_of)
        for (start, end, as_of), codes in sorted(grouped.items())
    )


def _outcome(row) -> KpiJobOutcome:
    refs = row["result_references"] or {}
    return KpiJobOutcome(row["id"], row["operation_id"], row["state"], refs.get("quarantine_state"))


def _request_ref(request: KpiRecalculationRequest) -> dict[str, object]:
    return {
        "kpi_codes": list(sorted(set(request.kpi_codes))),
        "period_start": request.period_start.isoformat(),
        "period_end": request.period_end.isoformat(),
        "as_of_date": request.as_of_date.isoformat(),
    }


def _requests_from_refs(references: dict[str, object]) -> tuple[KpiRecalculationRequest, ...]:
    return tuple(
        KpiRecalculationRequest(
            tuple(item["kpi_codes"]), date.fromisoformat(item["period_start"]),
            date.fromisoformat(item["period_end"]), date.fromisoformat(item["as_of_date"]),
        )
        for item in references.get("requests", [])
    )


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _month_end(value: date) -> date:
    return (_month_start(value).replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _as_utc_date(value: datetime) -> date:
    return value.astimezone(UTC).date()
