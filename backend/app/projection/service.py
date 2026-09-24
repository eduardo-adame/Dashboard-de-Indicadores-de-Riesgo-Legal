"""Proyección de snapshots certificados a entidades analíticas."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Mapping
from uuid import UUID, uuid5

from app.ingestion.models import SourceFamily
from app.projection.models import ProjectionContext, ProjectionError, ProjectionInvariantError, ProjectionResult
from app.validation.models import ValidatedTabularRecord


_OPERATION_NAMESPACE = UUID("c44b8c97-6e55-4a2e-9ba6-bf540fb5d26c")


@dataclass(frozen=True)
class _Target:
    entity_type: str
    table: str
    business_id: str
    values: dict[str, object]
    business_payload: dict[str, object]
    relationships: tuple[tuple[str, str, str | None], ...] = ()

    @property
    def payload_sha256(self) -> bytes:
        return canonical_payload_sha256(self.business_payload)


class ProjectionService:
    def __init__(self, repository) -> None:
        self.repository = repository

    def project(self, validated_record: ValidatedTabularRecord, context: ProjectionContext) -> tuple[ProjectionResult, ...]:
        with self.repository.transaction() as connection:
            return self.project_in_transaction(connection, validated_record, context)

    def project_in_transaction(self, connection, validated_record: ValidatedTabularRecord, context: ProjectionContext) -> tuple[ProjectionResult, ...]:
        provenance = self.repository.source_record_provenance(connection, validated_record.source_record_id)
        if provenance is None:
            raise ProjectionError("registro fuente inexistente")
        targets = _targets_for(validated_record)
        return tuple(
            self._project_target(connection, target, validated_record.source_record_id, provenance.ingest_file_id, context)
            for target in targets
        )

    def _project_target(self, connection, target: _Target, source_record_id: UUID, ingest_file_id: UUID, context: ProjectionContext) -> ProjectionResult:
        operation_id = derive_child_operation_id(context.operation_id, source_record_id, target.entity_type, target.business_id)
        payload_sha256 = target.payload_sha256
        existing = self.repository.application_for_target(
            connection, source_record_id=source_record_id, entity_type=target.entity_type, business_id=target.business_id
        )
        if existing is not None:
            if bytes(existing["payload_sha256"]) != payload_sha256:
                raise ProjectionInvariantError("la aplicación existente no coincide con el snapshot")
            result = "CONFLICTO" if existing["result"] == "CONFLICTO" else "IDEMPOTENTE"
            return _result(target, result, payload_sha256, source_record_id, operation_id, context.correlation_id)

        resolved = _resolve_relationships(connection, self.repository, target)
        inserted = self.repository.insert_entity(connection, table=resolved.table, values=resolved.values)
        if inserted:
            if self.repository.insert_application(
                connection, source_record_id=source_record_id, entity_type=resolved.entity_type,
                business_id=resolved.business_id, payload_sha256=payload_sha256, result="INCORPORADO",
                operation_id=operation_id, correlation_id=context.correlation_id,
            ):
                return _result(resolved, "INCORPORADO", payload_sha256, source_record_id, operation_id, context.correlation_id)
            return self._existing_after_race(connection, resolved, source_record_id, payload_sha256, operation_id, context)

        entity = self.repository.entity_row(connection, entity_type=resolved.entity_type, business_id=resolved.business_id)
        if entity is None:
            raise ProjectionError("la entidad analítica no pudo confirmarse")
        if bytes(entity["payload_sha256"]) == payload_sha256:
            if self.repository.insert_application(
                connection, source_record_id=source_record_id, entity_type=resolved.entity_type,
                business_id=resolved.business_id, payload_sha256=payload_sha256, result="IDEMPOTENTE",
                operation_id=operation_id, correlation_id=context.correlation_id,
            ):
                return _result(resolved, "IDEMPOTENTE", payload_sha256, source_record_id, operation_id, context.correlation_id)
            return self._existing_after_race(connection, resolved, source_record_id, payload_sha256, operation_id, context)

        if self.repository.insert_application(
            connection, source_record_id=source_record_id, entity_type=resolved.entity_type,
            business_id=resolved.business_id, payload_sha256=payload_sha256, result="CONFLICTO",
            operation_id=operation_id, correlation_id=context.correlation_id,
        ):
            self.repository.insert_conflict_quarantine(
                connection, ingest_file_id=ingest_file_id, source_record_id=source_record_id,
                entity_type=resolved.entity_type, business_id=resolved.business_id,
                original_payload=_json_object(entity), candidate_payload=resolved.business_payload,
                operation_id=derive_conflict_operation_id(context.operation_id, source_record_id, resolved.entity_type, resolved.business_id),
                correlation_id=context.correlation_id,
            )
            return _result(resolved, "CONFLICTO", payload_sha256, source_record_id, operation_id, context.correlation_id)
        return self._existing_after_race(connection, resolved, source_record_id, payload_sha256, operation_id, context)

    def _existing_after_race(self, connection, target: _Target, source_record_id: UUID, payload_sha256: bytes, operation_id: UUID, context: ProjectionContext) -> ProjectionResult:
        existing = self.repository.application_for_target(
            connection, source_record_id=source_record_id, entity_type=target.entity_type, business_id=target.business_id
        )
        if existing is None or bytes(existing["payload_sha256"]) != payload_sha256:
            raise ProjectionInvariantError("la aplicación concurrente no coincide con el snapshot")
        result = "CONFLICTO" if existing["result"] == "CONFLICTO" else "IDEMPOTENTE"
        return _result(target, result, payload_sha256, source_record_id, operation_id, context.correlation_id)


def derive_child_operation_id(parent_operation_id: UUID, source_record_id: UUID, entity_type: str, business_id: str) -> UUID:
    return uuid5(_OPERATION_NAMESPACE, f"{parent_operation_id}:{source_record_id}:{entity_type}:{business_id}")


def derive_conflict_operation_id(parent_operation_id: UUID, source_record_id: UUID, entity_type: str, business_id: str) -> UUID:
    return uuid5(_OPERATION_NAMESPACE, f"conflict:{parent_operation_id}:{source_record_id}:{entity_type}:{business_id}")


def canonical_payload_sha256(payload: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(_canonical_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ProjectionError("payload de negocio no representable") from exc
    return hashlib.sha256(encoded.encode("utf-8")).digest()


def _canonical_value(value: object) -> object:
    if value is None or isinstance(value, str) or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ProjectionError("decimal no representable")
        normalized = value.normalize()
        return format(normalized, "f")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    raise ProjectionError("tipo de payload no representable")


def _targets_for(record: ValidatedTabularRecord) -> tuple[_Target, ...]:
    values = record.values_by_name
    source_id = record.source_record_id
    if record.family == SourceFamily.CONTRACTS_DOCUMENTS:
        contract_id = _text(values, "ID_Contrato")
        targets = [_Target("CONTRATO", "contract_record", contract_id, {
            "id_contrato": contract_id, "fecha_solicitud": _date(values, "Fecha_Solicitud"),
            "fecha_firma": _optional_date(values, "Fecha_Firma"), "fecha_vencimiento": _date(values, "Fecha_Vencimiento"),
            "estado_revision": _text(values, "Estado_Revision"), "payload_sha256": b"", "source_record_id": source_id,
        }, _business(values, "ID_Contrato", "Fecha_Solicitud", "Fecha_Firma", "Fecha_Vencimiento", "Estado_Revision"))]
        if _complete(values, "ID_Asunto", "Tipo_Asunto", "Estado", "Fecha"):
            targets.append(_legal_matter(values, source_id, "CONTRATO", contract_id))
        return tuple(_with_hash(target) for target in targets)
    if record.family == SourceFamily.LITIGATION:
        litigation_id = _text(values, "ID_Litigio")
        contract_id = _optional_text(values, "ID_Contrato")
        targets = [_Target("LITIGIO", "litigation", litigation_id, {
            "id_litigio": litigation_id, "fecha_apertura": _date(values, "Fecha_Apertura"), "estado": _text(values, "Estado"),
            "nivel_severidad": _text(values, "Nivel_Severidad"), "monto_reclamado": _optional_decimal(values, "Monto_Reclamado"),
            "estimacion_interna": _optional_decimal(values, "Estimacion_Interna"), "id_contrato": None,
            "payload_sha256": b"", "source_record_id": source_id,
        }, _business(values, "ID_Litigio", "Fecha_Apertura", "Estado", "Nivel_Severidad", "Monto_Reclamado", "Estimacion_Interna"), (("id_contrato", "contract_record", contract_id),))]
        if _complete(values, "ID_Asunto", "Tipo_Asunto", "Estado", "Fecha"):
            targets.append(_legal_matter(values, source_id, "LITIGIO", litigation_id))
        return tuple(_with_hash(target) for target in targets)
    if record.family == SourceFamily.COMPLIANCE:
        obligation_id = _text(values, "ID_Obligacion")
        contract_id = _optional_text(values, "ID_Contrato")
        targets = [_Target("OBLIGACION", "compliance_obligation", obligation_id, {
            "id_obligacion": obligation_id, "fecha_limite": _date(values, "Fecha_Limite"),
            "evidencia_cumplimiento": _optional_text(values, "Evidencia_Cumplimiento"), "id_contrato": None,
            "payload_sha256": b"", "source_record_id": source_id,
        }, _business(values, "ID_Obligacion", "Fecha_Limite", "Evidencia_Cumplimiento"), (("id_contrato", "contract_record", contract_id),))]
        if _complete(values, "ID_Incidente", "Fecha_Evento", "Area", "Nivel_Severidad"):
            targets.append(_incident(values, source_id, obligation_id))
        if _complete(values, "ID_Asunto", "Tipo_Asunto", "Estado", "Fecha"):
            targets.append(_legal_matter(values, source_id, "OBLIGACION", obligation_id))
        return tuple(_with_hash(target) for target in targets)
    if record.family == SourceFamily.INTERNAL_AUDIT:
        targets: list[_Target] = []
        if _complete(values, "ID_Incidente", "Fecha_Evento", "Area", "Nivel_Severidad"):
            targets.append(_incident(values, source_id, _optional_text(values, "ID_Obligacion")))
        if _complete(values, "ID_Asunto", "Tipo_Asunto", "Estado", "Fecha"):
            targets.append(_legal_matter(values, source_id, "AUDITORIA_INTERNA", _text(values, "ID_Asunto")))
        return tuple(_with_hash(target) for target in targets)
    raise ProjectionError("familia certificada sin mapping analítico")


def _legal_matter(values: Mapping[str, object], source_record_id: UUID, source_type: str, source_business_id: str) -> _Target:
    matter_id = _text(values, "ID_Asunto")
    return _Target("ASUNTO", "legal_matter", matter_id, {
        "id_asunto": matter_id, "tipo_asunto": _text(values, "Tipo_Asunto"), "estado": _text(values, "Estado"),
        "fecha": _date(values, "Fecha"), "source_entity_type": source_type, "source_business_id": source_business_id,
        "payload_sha256": b"", "source_record_id": source_record_id,
    }, _business(values, "ID_Asunto", "Tipo_Asunto", "Estado", "Fecha"))


def _incident(values: Mapping[str, object], source_record_id: UUID, obligation_id: str | None) -> _Target:
    incident_id = _text(values, "ID_Incidente")
    return _Target("INCIDENTE", "incident", incident_id, {
        "id_incidente": incident_id, "fecha_evento": _date(values, "Fecha_Evento"), "area": _text(values, "Area"),
        "nivel_severidad": _text(values, "Nivel_Severidad"), "id_obligacion": None,
        "payload_sha256": b"", "source_record_id": source_record_id,
    }, _business(values, "ID_Incidente", "Fecha_Evento", "Area", "Nivel_Severidad"), (("id_obligacion", "compliance_obligation", obligation_id),))


def _with_hash(target: _Target) -> _Target:
    values = dict(target.values)
    values["payload_sha256"] = target.payload_sha256
    return replace(target, values=values)


def _resolve_relationships(connection, repository, target: _Target) -> _Target:
    values = dict(target.values)
    for column, table, identifier in target.relationships:
        values[column] = identifier if identifier and repository.related_exists(connection, table=table, identifier=identifier) else None
    return replace(target, values=values)


def _result(target: _Target, result: str, payload_sha256: bytes, source_record_id: UUID, operation_id: UUID, correlation_id: UUID) -> ProjectionResult:
    return ProjectionResult(target.entity_type, target.business_id, result, payload_sha256, source_record_id, operation_id, correlation_id, result == "INCORPORADO")


def _complete(values: Mapping[str, object], *names: str) -> bool:
    return all(name in values and values[name] is not None and (not isinstance(values[name], str) or bool(values[name].strip())) for name in names)


def _text(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProjectionError(f"campo de negocio no representable: {name}")
    return value.strip()


def _optional_text(values: Mapping[str, object], name: str) -> str | None:
    value = values.get(name)
    if value is None or value == "":
        return None
    return _text(values, name)


def _date(values: Mapping[str, object], name: str) -> date:
    value = values.get(name)
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ProjectionError(f"fecha no representable: {name}") from exc
    raise ProjectionError(f"fecha no representable: {name}")


def _optional_date(values: Mapping[str, object], name: str) -> date | None:
    return None if values.get(name) in (None, "") else _date(values, name)


def _optional_decimal(values: Mapping[str, object], name: str) -> Decimal | None:
    value = values.get(name)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ProjectionError(f"decimal no representable: {name}")
    try:
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProjectionError(f"decimal no representable: {name}") from exc
    if not decimal.is_finite():
        raise ProjectionError(f"decimal no representable: {name}")
    return decimal


def _business(values: Mapping[str, object], *names: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in names:
        value = values.get(name)
        if name.startswith("Fecha") and value not in (None, ""):
            result[name] = _date(values, name)
        elif name in {"Monto_Reclamado", "Estimacion_Interna"}:
            result[name] = _optional_decimal(values, name)
        elif value is None:
            result[name] = None
        elif isinstance(value, str):
            result[name] = _text(values, name)
        else:
            result[name] = value
    return result


def _json_object(row: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if key not in {"payload_sha256", "source_record_id", "created_at", "updated_at"}}
