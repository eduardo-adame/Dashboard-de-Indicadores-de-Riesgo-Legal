"""Caso de uso de coordinación: reclama, ejecuta downstream y confirma.

La identidad funcional estable es ``coordination_dispatch.operation_id`` y se
propaga al ``OperationContext`` downstream. El procesamiento downstream ocurre
fuera de toda transacción de coordinación; el ciclo de vida usa transacciones
cortas e independientes para el claim, el heartbeat, el éxito y el fallo.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.coordination.models import (
    CoordinationError,
    DispatchConflictError,
    DispatchState,
)
from app.security.models import AuthenticatedPrincipal


@dataclass(frozen=True)
class DispatchContext:
    operation_id: UUID
    correlation_id: UUID
    actor: AuthenticatedPrincipal


@dataclass(frozen=True)
class DispatchOutcome:
    downstream_target: str | None
    operation_id: UUID | None
    downstream_result_id: UUID | None
    state: str


class CoordinationService:
    def __init__(
        self,
        repository,
        security,
        *,
        validation_runner=None,
        document_runner=None,
        stale_threshold_seconds: int = 3600,
    ) -> None:
        self.repository = repository
        self.security = security
        self._validation_runner = validation_runner
        self._document_runner = document_runner
        self._stale_threshold_seconds = stale_threshold_seconds

    def dispatch(
        self,
        *,
        file_id: UUID,
        correlation_id: UUID,
        actor: AuthenticatedPrincipal,
        capability: str = "ingest.execute",
    ) -> DispatchOutcome:
        # TX corta: autorización + reconstrucción de destino + get-or-create + claim.
        with self.repository.transaction() as connection:
            self._authorize(connection, actor, capability, correlation_id)
            target = self.repository.reconstruct_routing_target(connection, file_id)
            if target is None:
                return DispatchOutcome(None, None, None, "SKIPPED")

            dispatch = self.repository.get_or_create_dispatch(
                connection,
                file_id=file_id,
                downstream_target=target.value,
                correlation_id=correlation_id,
            )
            if dispatch.state == DispatchState.COMPLETED:
                return DispatchOutcome(target.value, dispatch.operation_id, dispatch.downstream_result_id, "COMPLETED")

            if dispatch.state == DispatchState.IN_PROGRESS:
                self.repository.recover_stale(connection, self._stale_threshold_seconds)
                dispatch = self.repository.get(connection, dispatch.id)
                if dispatch is not None and dispatch.state == DispatchState.IN_PROGRESS:
                    raise DispatchConflictError("el despacho ya está en curso")

            if not self.repository.claim(connection, dispatch.id):
                raise DispatchConflictError("no se pudo reclamar el despacho")
            dispatch_id = dispatch.id
            operation_id = dispatch.operation_id
            target_value = target.value

        # Procesamiento downstream: SIN transacción de coordinación abierta.
        try:
            result_id = self._run_downstream(target_value, file_id, operation_id, correlation_id, actor)
        except Exception as exc:  # noqa: BLE001 - se propaga tras registrar el fallo
            with self.repository.transaction() as connection:
                self.repository.fail(connection, dispatch_id, safe_cause_code=exc.__class__.__name__.upper())
            raise

        # TX corta: éxito.
        with self.repository.transaction() as connection:
            self.repository.complete(connection, dispatch_id, result_id)
        return DispatchOutcome(target_value, operation_id, result_id, "COMPLETED")

    def _run_downstream(
        self,
        target_value: str,
        file_id: UUID,
        operation_id: UUID,
        correlation_id: UUID,
        actor: AuthenticatedPrincipal,
    ) -> UUID | None:
        context = DispatchContext(operation_id=operation_id, correlation_id=correlation_id, actor=actor)
        if target_value == "VALIDATION":
            if self._validation_runner is None:
                raise CoordinationError("runner de validación no configurado")
            return self._validation_runner(context, file_id)
        if target_value == "DOCUMENT":
            if self._document_runner is None:
                raise CoordinationError("runner documental no configurado")
            return self._document_runner(context, file_id)
        raise CoordinationError(f"destino desconocido: {target_value}")

    def recover_stale(self) -> list[UUID]:
        with self.repository.transaction() as connection:
            return self.repository.recover_stale(connection, self._stale_threshold_seconds)

    def _authorize(self, connection, actor, capability, correlation_id) -> None:
        try:
            self.security.revalidate_functional_access(connection, actor, capability)
        except Exception as exc:  # noqa: BLE001 - se registra la denegación y se propaga
            self.repository.write_audit_event(
                connection,
                actor=actor,
                action="COORDINATION_DENIED",
                resource_type="COORDINATION_DISPATCH",
                resource_identifier=None,
                result="DENIED",
                correlation_id=correlation_id,
                safe_cause_code=exc.__class__.__name__.upper(),
            )
            raise
