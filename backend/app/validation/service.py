"""Casos de uso del módulo de validación y cuarentena.

Orquesta la validación estructural y por registro y materializa la cuarentena en
una única transacción. La persistencia se delega en un repositorio con el mismo
patrón transaccional del resto del backend; la revalidación funcional se realiza
sobre la conexión del consumidor.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from types import MappingProxyType
from uuid import UUID

from app.ingestion.models import SourceFamily
from app.security.models import AuthenticatedPrincipal, SecurityError
from app.validation.models import (
    QuarantineCause,
    ValidatedTabularRecord,
    ValidationInvocationError,
    ValidationResult,
)
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


@dataclass(frozen=True)
class ReinjectionValidationResult:
    quarantine_item: QuarantineItem
    validated_record: ValidatedTabularRecord | None


class _SnapshotFreezeError(ValueError):
    pass


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
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            authoritative_family = self.repository.family_for_file(connection, file_id)
            if family != authoritative_family:
                raise ValidationInvocationError("familia de invocación no coincide con el archivo")
            structural = validate_structure_for_family(
                headers,
                ((record.position, record.width) for record in records),
                authoritative_family,
            )
            if not structural.conforming:
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
                    file_id=str(file_id), family=authoritative_family, structural=structural,
                    conforming_positions=(), quarantined=(),
                )
            positions = tuple(record.position for record in records)
            if len(positions) != len(set(positions)):
                raise ValidationInvocationError("posiciones de registro duplicadas")
            for record in records:
                if not self.repository.source_record_matches(
                    connection,
                    source_record_id=record.source_record_id,
                    file_id=file_id,
                    position=record.position,
                ):
                    raise ValidationInvocationError("registro fuente no coincide con archivo y posición")

            validated_records: list[ValidatedTabularRecord] = []
            quarantined: list[tuple[int, QuarantineCause]] = []
            for record in records:
                try:
                    frozen_values = _freeze_mapping(record.values_by_name)
                except _SnapshotFreezeError:
                    self._quarantine_invalid_type(
                        connection, file_id, record, context, quarantined
                    )
                    continue
                outcome = validate_record_for_family(frozen_values, authoritative_family)
                if outcome.conforming:
                    validated_records.append(
                        ValidatedTabularRecord(
                            source_record_id=record.source_record_id,
                            family=authoritative_family,
                            position=record.position,
                            values_by_name=frozen_values,
                        )
                    )
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
                        original_payload=_json_compatible(frozen_values),
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
                file_id=str(file_id), family=authoritative_family, structural=structural,
                conforming_positions=tuple(record.position for record in validated_records),
                quarantined=tuple(quarantined), validated_records=tuple(validated_records),
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
        return self._reinject_core(
            item_id=item_id, corrected_payload=corrected_payload, context=context,
            capability=capability,
        ).quarantine_item

    def reinject_with_validated_record(
        self,
        *,
        item_id: UUID,
        corrected_payload: dict[str, object],
        context: ValidationContext,
        capability: str = "quarantine.reinject",
    ) -> ReinjectionValidationResult:
        return self._reinject_core(
            item_id=item_id, corrected_payload=corrected_payload, context=context,
            capability=capability,
        )

    def _reinject_core(
        self, *, item_id: UUID, corrected_payload: dict[str, object], context: ValidationContext,
        capability: str,
    ) -> ReinjectionValidationResult:
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            item = self.repository.load_quarantine(connection, item_id, for_update=True)
            if item is None:
                raise QuarantineError("elemento de cuarentena inexistente")
            candidate = _reinject(item, corrected_payload)
            provenance = self.repository.provenance_for_quarantine(connection, item)
            try:
                frozen_values = _freeze_mapping(corrected_payload)
            except _SnapshotFreezeError:
                frozen_values = None
                outcome = validate_record_for_family({}, provenance.family)
                outcome = outcome.__class__(False, QuarantineCause.INVALID_TYPE)
            else:
                outcome = validate_record_for_family(frozen_values, provenance.family)
            if outcome.conforming:
                updated = mark_reinjected(candidate)
                snapshot = ValidatedTabularRecord(
                    source_record_id=provenance.source_record_id,
                    family=provenance.family,
                    position=provenance.position,
                    values_by_name=frozen_values,
                )
            else:
                updated = keep_pending(candidate, outcome.cause, _json_compatible_or_raise(corrected_payload))
                snapshot = None
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
            return ReinjectionValidationResult(updated, snapshot)

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

    def _quarantine_invalid_type(self, connection, file_id, record, context, quarantined) -> None:
        payload = _json_compatible_or_raise(record.values_by_name)
        child_operation_id = derive_record_operation_id(context.operation_id, record.source_record_id)
        existing = self.repository.find_quarantine_by_operation(connection, child_operation_id)
        if existing is None:
            self.repository.insert_quarantine(
                connection, file_id=file_id, source_record_id=record.source_record_id,
                cause=QuarantineCause.INVALID_TYPE, operation_id=child_operation_id,
                correlation_id=context.correlation_id, original_payload=payload,
            )
            self.repository.write_audit_event(
                connection, actor=context.actor, action="QUARANTINE_CREATED",
                resource_type="QUARANTINE_ITEM", resource_identifier=str(child_operation_id),
                result="SUCCESS", correlation_id=context.correlation_id,
                safe_cause_code=QuarantineCause.INVALID_TYPE.value,
            )
        quarantined.append((record.position, QuarantineCause.INVALID_TYPE))


def _freeze_mapping(values: Mapping[str, object]) -> Mapping[str, object]:
    frozen = _freeze(values, set())
    if not isinstance(frozen, Mapping):
        raise _SnapshotFreezeError("el payload no es un mapping")
    return frozen


def _freeze(value: object, active: set[int]) -> object:
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise _SnapshotFreezeError("estructura cíclica")
        active.add(marker)
        try:
            return MappingProxyType({key: _freeze(item, active) for key, item in value.items()})
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in active:
            raise _SnapshotFreezeError("estructura cíclica")
        active.add(marker)
        try:
            items = tuple(_freeze(item, active) for item in value)
        finally:
            active.remove(marker)
        return frozenset(items) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, (str, bytes, int, float, bool, type(None), date, datetime, Decimal, UUID)):
        return value
    raise _SnapshotFreezeError("tipo mutable no soportado")


def _json_compatible(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [_json_compatible(item) for item in value]
    return value


def _json_compatible_or_raise(value: object) -> dict[str, object]:
    try:
        payload = _json_compatible(value)
        json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValidationInvocationError("payload no persistible") from exc
    if not isinstance(payload, dict):
        raise ValidationInvocationError("payload no persistible")
    return payload
