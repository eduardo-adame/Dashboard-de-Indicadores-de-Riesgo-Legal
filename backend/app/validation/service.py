"""Casos de uso del módulo de validación y cuarentena.

Orquesta la validación estructural y por registro y materializa la cuarentena en
una única transacción. La persistencia se delega en un repositorio con el mismo
patrón transaccional del resto del backend; la revalidación funcional se realiza
sobre la conexión del consumidor.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.ingestion.models import SourceFamily
from app.security.models import AuthenticatedPrincipal, SecurityError
from app.validation.models import QuarantineCause, ValidationResult
from app.validation.quarantine import (
    QuarantineError,
    QuarantineItem,
    QuarantineTransition,
    discard as _discard,
    keep_pending,
    mark_reinjected,
    reinject as _reinject,
)
from app.validation.records import derive_record_operation_id, validate_record_for_family
from app.validation.structural import validate_structure_for_family


@dataclass(frozen=True)
class TabularRecord:
    """Registro tabular con su identidad fuente y posición de origen."""

    position: int
    source_record_id: UUID
    values_by_name: dict[str, object]
    width: int


@dataclass(frozen=True)
class ValidationContext:
    """Contexto de la operación de validación."""

    operation_id: UUID
    correlation_id: UUID
    actor: AuthenticatedPrincipal


class ValidationService:
    def __init__(self, repository, security) -> None:
        self.repository = repository
        self.security = security

    # -- validación ---------------------------------------------------------
    def validate(
        self,
        *,
        file_id: UUID,
        family: SourceFamily,
        headers: tuple[object, ...],
        records: tuple[TabularRecord, ...],
        context: ValidationContext,
        capability: str = "ingest.execute",
    ) -> ValidationResult:
        structural = validate_structure_for_family(
            headers,
            ((record.position, record.width) for record in records),
            family,
        )

        if not structural.conforming:
            with self.repository.transaction() as connection:
                self._authorize(connection, context, capability)
                self.repository.write_audit_event(
                    connection,
                    actor=context.actor,
                    action="VALIDATION_FILE_REJECTED",
                    resource_type="INGEST_FILE",
                    resource_identifier=str(file_id),
                    result="REJECTED",
                    correlation_id=context.correlation_id,
                    safe_cause_code=structural.cause.value if structural.cause else None,
                )
            return ValidationResult(
                file_id=str(file_id),
                family=family,
                structural=structural,
                conforming_positions=(),
                quarantined=(),
            )

        conforming: list[int] = []
        quarantined: list[tuple[int, QuarantineCause]] = []
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            for record in records:
                outcome = validate_record_for_family(record.values_by_name, family)
                if outcome.conforming:
                    conforming.append(record.position)
                    continue

                child_operation_id = derive_record_operation_id(
                    context.operation_id, record.source_record_id
                )
                existing = self.repository.find_quarantine_by_operation(connection, child_operation_id)
                if existing is None:
                    self.repository.insert_quarantine(
                        connection,
                        file_id=file_id,
                        source_record_id=record.source_record_id,
                        cause=outcome.cause,
                        operation_id=child_operation_id,
                        correlation_id=context.correlation_id,
                        original_payload=record.values_by_name,
                    )
                    self.repository.write_audit_event(
                        connection,
                        actor=context.actor,
                        action="QUARANTINE_CREATED",
                        resource_type="QUARANTINE_ITEM",
                        resource_identifier=str(child_operation_id),
                        result="SUCCESS",
                        correlation_id=context.correlation_id,
                        safe_cause_code=outcome.cause.value if outcome.cause else None,
                    )
                quarantined.append((record.position, outcome.cause))

        return ValidationResult(
            file_id=str(file_id),
            family=family,
            structural=structural,
            conforming_positions=tuple(conforming),
            quarantined=tuple(quarantined),
        )

    # -- reinyección --------------------------------------------------------
    def reinject(
        self,
        *,
        item_id: UUID,
        corrected_payload: dict[str, object],
        context: ValidationContext,
        capability: str = "quarantine.reinject",
    ) -> QuarantineItem:
        contract_family = None
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            item = self.repository.load_quarantine(connection, item_id, for_update=True)
            if item is None:
                raise QuarantineError("elemento de cuarentena inexistente")
            candidate = _reinject(item, corrected_payload)
            contract_family = self._family_for(connection, item)
            outcome = validate_record_for_family(corrected_payload, contract_family)
            if outcome.conforming:
                updated = mark_reinjected(candidate)
            else:
                updated = keep_pending(candidate, outcome.cause, corrected_payload)
            self.repository.update_quarantine(connection, updated)
            self.repository.insert_transition(
                connection,
                QuarantineTransition(
                    item_id=item.id,
                    from_state=item.state,
                    to_state=updated.state,
                    operation_id=context.operation_id,
                    correlation_id=context.correlation_id,
                    actor_identifier=context.actor.username,
                ),
            )
            self.repository.write_audit_event(
                connection,
                actor=context.actor,
                action="QUARANTINE_REINJECT",
                resource_type="QUARANTINE_ITEM",
                resource_identifier=str(item.id),
                result=updated.state.value.upper(),
                correlation_id=context.correlation_id,
                safe_cause_code=None if outcome.conforming else outcome.cause.value,
            )
            return updated

    # -- descarte -----------------------------------------------------------
    def discard(
        self,
        *,
        item_id: UUID,
        justification: str,
        context: ValidationContext,
        capability: str = "quarantine.discard",
    ) -> QuarantineItem:
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            item = self.repository.load_quarantine(connection, item_id, for_update=True)
            if item is None:
                raise QuarantineError("elemento de cuarentena inexistente")
            updated = _discard(item, justification)
            self.repository.update_quarantine(connection, updated)
            self.repository.insert_transition(
                connection,
                QuarantineTransition(
                    item_id=item.id,
                    from_state=item.state,
                    to_state=updated.state,
                    operation_id=context.operation_id,
                    correlation_id=context.correlation_id,
                    actor_identifier=context.actor.username,
                ),
            )
            self.repository.write_audit_event(
                connection,
                actor=context.actor,
                action="QUARANTINE_DISCARD",
                resource_type="QUARANTINE_ITEM",
                resource_identifier=str(item.id),
                result="SUCCESS",
                correlation_id=context.correlation_id,
                safe_cause_code=None,
            )
            return updated

    # -- consulta de cuarentena ---------------------------------------------
    def list_quarantine(
        self,
        *,
        context: ValidationContext,
        state: str | None = None,
        ingest_file_id: UUID | None = None,
        cause: str | None = None,
        capability: str = "quarantine.read",
    ) -> list[QuarantineItem]:
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            return self.repository.list_quarantine(
                connection, state=state, ingest_file_id=ingest_file_id, cause=cause
            )

    # -- internos -----------------------------------------------------------
    def _authorize(self, connection, context: ValidationContext, capability: str) -> None:
        try:
            self.security.revalidate_functional_access(connection, context.actor, capability)
        except SecurityError as exc:
            self.repository.write_audit_event(
                connection,
                actor=context.actor,
                action="VALIDATION_DENIED",
                resource_type="VALIDATION",
                resource_identifier=None,
                result="DENIED",
                correlation_id=context.correlation_id,
                safe_cause_code=exc.__class__.__name__.upper(),
            )
            raise

    def _family_for(self, connection, item: QuarantineItem) -> SourceFamily:
        return self.repository.family_for_quarantine(connection, item)
