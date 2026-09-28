"""Orquestación durable del análisis proactivo programado."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from app.analytics.models import (
    ProactiveAnalysisContext,
    ProactiveAnalysisError,
    ProactiveAnalysisResult,
)
from app.analytics.proactive_rules import valid_fingerprint_contract
from app.analytics.repository import AnalyticsRepository
from app.analytics.service import ProactiveAnalysisService
from app.coordination.proactive_repository import (
    CoreDurableStateGap,
    ProactiveIntegrationRepository,
)


_PROACTIVE_NAMESPACE = UUID("59737249-7e04-4cba-bebd-5115f94d58db")
_CORRELATION_NAMESPACE = UUID("8a5d92af-79e4-4644-8752-cc02e8f52c4b")
_PROCESS_IDENTIFIER = "controlled_ingestion.proactive_analysis"
_CONTRACT = "SCHEDULED_PROACTIVE_ANALYSIS_V1"
_TRIGGER = "CONTROLLED_INGESTION"
_DAG_ID = "controlled_ingestion"
_TERMINAL_STATES = frozenset({"COMPLETED", "NO_RELEVANT_WORK"})


class ProactiveIntegrationError(RuntimeError):
    """La ejecución proactiva programada no pudo confirmarse."""


@dataclass(frozen=True)
class ProactiveJobOutcome:
    job_id: UUID
    operation_id: UUID
    state: str


def canonical_interval(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("el inicio del intervalo requiere zona horaria")
    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError("el fin del intervalo requiere zona horaria")
    canonical_start, canonical_end = start.astimezone(UTC), end.astimezone(UTC)
    if canonical_end <= canonical_start:
        raise ValueError("el intervalo programado no es válido")
    return canonical_start, canonical_end


def derive_proactive_operation_id(*, interval_start: datetime, interval_end: datetime) -> UUID:
    start, end = canonical_interval(interval_start, interval_end)
    return uuid5(
        _PROACTIVE_NAMESPACE,
        f"{_DAG_ID}:{start.isoformat()}:{end.isoformat()}:PROACTIVE_ANALYSIS",
    )


def derive_proactive_correlation_id(*, interval_start: datetime, interval_end: datetime) -> UUID:
    start, end = canonical_interval(interval_start, interval_end)
    return uuid5(
        _CORRELATION_NAMESPACE,
        f"{_DAG_ID}:{start.isoformat()}:{end.isoformat()}",
    )


class ProactiveIntegrationService:
    def __init__(self, *, conninfo: str, audit_repository, repository) -> None:
        self.repository = repository
        self.audit_repository = audit_repository
        self.core = ProactiveAnalysisService(
            AnalyticsRepository(conninfo),
            audit_repository,
        )

    def schedule(
        self,
        *,
        interval_start: datetime,
        interval_end: datetime,
        correlation_id: UUID,
    ) -> ProactiveJobOutcome:
        start, end = canonical_interval(interval_start, interval_end)
        operation_id = derive_proactive_operation_id(
            interval_start=start,
            interval_end=end,
        )
        references = _trigger_references(start, end)

        with self.repository.transaction() as connection:
            self.repository.lock_operation(connection, operation_id)
            job = self.repository.job_for_operation(
                connection, operation_id, for_update=True
            )
            if job is None:
                self.repository.create_job(
                    connection,
                    operation_id=operation_id,
                    correlation_id=correlation_id,
                    actor_process=_PROCESS_IDENTIFIER,
                    window_start=start,
                    window_end=end,
                    references=references,
                )

        with self.repository.execution_lock(operation_id):
            return self._execute_locked(operation_id)

    def _execute_locked(self, operation_id: UUID) -> ProactiveJobOutcome:
        with self.repository.transaction() as connection:
            job = self.repository.job_for_operation(
                connection, operation_id, for_update=True
            )
            if job is None:
                raise ProactiveIntegrationError("el job proactivo no existe")
            _validate_trigger_references(job["result_references"])
            if job["state"] in _TERMINAL_STATES:
                return _outcome(job)
            if job["state"] == "FAILED":
                self.repository.restart_job(connection, job["id"])
            authoritative = {
                "id": job["id"],
                "operation_id": job["operation_id"],
                "correlation_id": job["correlation_id"],
                "actor_process": job["actor_process"],
            }

        context = ProactiveAnalysisContext(
            authoritative["id"],
            authoritative["operation_id"],
            authoritative["correlation_id"],
            authoritative["actor_process"],
        )
        try:
            result = self.core.execute(context)
        except Exception as exc:
            reconciled = self._reconcile_failure(operation_id, exc)
            if reconciled is not None:
                return reconciled
            raise ProactiveIntegrationError(
                "la ejecución proactiva no pudo confirmarse"
            ) from exc
        try:
            return self._finalize_result(operation_id, result)
        except (ProactiveIntegrationError, CoreDurableStateGap):
            raise
        except Exception as exc:
            raise ProactiveIntegrationError(
                "la finalización durable no pudo confirmarse"
            ) from exc

    def _finalize_result(
        self,
        operation_id: UUID,
        result: ProactiveAnalysisResult,
    ) -> ProactiveJobOutcome:
        with self.repository.transaction() as connection:
            job = self.repository.job_for_operation(
                connection, operation_id, for_update=True
            )
            if job is None:
                raise ProactiveIntegrationError("el job proactivo no existe")
            if job["state"] in _TERMINAL_STATES:
                return _outcome(job)
            references = _terminal_references(job["result_references"], result)
            if result.status == "COMPLETED":
                self.repository.finalize_job(
                    connection,
                    job["id"],
                    state="COMPLETED",
                    references=references,
                )
            elif result.status == "NO_RELEVANT_WORK":
                self.audit_repository.write_audit_event(
                    connection,
                    actor=None,
                    action="PROACTIVE_ANALYSIS",
                    resource_type="JOB_RUN",
                    resource_identifier=str(job["id"]),
                    result="NO_RELEVANT_WORK",
                    correlation_id=job["correlation_id"],
                    process_identifier=job["actor_process"],
                )
                self.repository.finalize_job(
                    connection,
                    job["id"],
                    state="NO_RELEVANT_WORK",
                    references=references,
                )
            else:
                raise ProactiveIntegrationError("resultado CORE no reconocido")
            return ProactiveJobOutcome(job["id"], operation_id, result.status)

    def _reconcile_failure(
        self,
        operation_id: UUID,
        cause: Exception,
    ) -> ProactiveJobOutcome | None:
        safe_cause = _safe_cause(cause)
        with self.repository.transaction() as connection:
            job = self.repository.job_for_operation(
                connection, operation_id, for_update=True
            )
            if job is None:
                raise ProactiveIntegrationError("el job proactivo no existe")
            run = self.repository.linked_core_run(connection, job)
            if run is None:
                self.audit_repository.write_audit_event(
                    connection,
                    actor=None,
                    action="PROACTIVE_ANALYSIS",
                    resource_type="JOB_RUN",
                    resource_identifier=str(job["id"]),
                    result="FAILURE",
                    correlation_id=job["correlation_id"],
                    safe_cause_code=safe_cause,
                    process_identifier=job["actor_process"],
                )
                self.repository.finalize_job(
                    connection,
                    job["id"],
                    state="FAILED",
                    references=dict(job["result_references"]),
                    safe_error_code=safe_cause,
                )
                return None

            if run["state"] == "COMPLETED":
                references = _references_from_run(job["result_references"], run)
                self.repository.finalize_job(
                    connection,
                    job["id"],
                    state="COMPLETED",
                    references=references,
                )
                return ProactiveJobOutcome(job["id"], operation_id, "COMPLETED")
            if run["state"] == "FAILED":
                persisted_cause = run["not_evaluated_reason"] or safe_cause
                self.repository.finalize_job(
                    connection,
                    job["id"],
                    state="FAILED",
                    references=dict(job["result_references"]),
                    safe_error_code=str(persisted_cause)[:120],
                )
                return None
            if run["state"] == "STARTED":
                return None
            raise CoreDurableStateGap("estado de run proactivo no reconocido")


def _trigger_references(start: datetime, end: datetime) -> dict[str, object]:
    return {
        "contract": _CONTRACT,
        "trigger": _TRIGGER,
        "dag_id": _DAG_ID,
        "data_interval_start": start.isoformat(),
        "data_interval_end": end.isoformat(),
    }


def _validate_trigger_references(references: object) -> None:
    if not isinstance(references, dict):
        raise ProactiveIntegrationError("referencias del job no válidas")
    if (
        references.get("contract") != _CONTRACT
        or references.get("trigger") != _TRIGGER
        or references.get("dag_id") != _DAG_ID
        or not references.get("data_interval_start")
        or not references.get("data_interval_end")
    ):
        raise ProactiveIntegrationError("identidad durable del trigger no válida")


def _terminal_references(
    initial: object,
    result: ProactiveAnalysisResult,
) -> dict[str, object]:
    _validate_trigger_references(initial)
    references = dict(initial)
    references["status"] = result.status
    references["input_fingerprint_sha256"] = result.input_fingerprint_sha256
    if result.status == "COMPLETED":
        if result.analytic_run_id is None or result.run_operation_id is None:
            raise ProactiveIntegrationError("resultado CORE incompleto")
        references["analytic_run_id"] = str(result.analytic_run_id)
        references["run_operation_id"] = str(result.run_operation_id)
    return references


def _references_from_run(initial: object, run) -> dict[str, object]:
    _validate_trigger_references(initial)
    rules_reference = run["rules_reference"] or {}
    if not valid_fingerprint_contract(rules_reference):
        raise CoreDurableStateGap("el run completado no contiene una huella válida")
    references = dict(initial)
    references.update(
        status="COMPLETED",
        analytic_run_id=str(run["id"]),
        run_operation_id=str(run["operation_id"]),
        input_fingerprint_sha256=rules_reference["input_fingerprint_sha256"],
    )
    return references


def _safe_cause(cause: Exception) -> str:
    if isinstance(cause, ProactiveAnalysisError):
        return cause.safe_cause_code[:120]
    return "PROACTIVE_ANALYSIS_FAILED"


def _outcome(job) -> ProactiveJobOutcome:
    return ProactiveJobOutcome(job["id"], job["operation_id"], job["state"])
