"""Emisión mínima de procesos del servidor sin lectura ni modificación de bitácora."""
from __future__ import annotations

import json
from uuid import UUID, uuid5

import psycopg

from app.security.models import AuditPersistenceError


_NAMESPACE = UUID("b2f7d54a-1480-47c2-99b9-fc6c872b57ef")


def write_process_event(
    connection, *, process_identifier: str, action: str, resource_type: str,
    resource_identifier: str, operation_id: UUID, correlation_id: UUID,
    result: str, safe_cause_code: str | None = None, attempt_number: int = 1,
) -> UUID:
    """Reentrega un evento lógico usando sólo INSERT y la transacción del productor."""
    identity = json.dumps([
        process_identifier, action, resource_type, resource_identifier,
        str(operation_id), str(correlation_id), result, safe_cause_code, attempt_number,
    ], ensure_ascii=True, separators=(",", ":"))
    event_id = uuid5(_NAMESPACE, identity)
    try:
        # Sin SELECT, RETURNING ni UPDATE: runtime conserva sus privilegios INSERT-only.
        connection.execute(
            """INSERT INTO audit.event
               (id, actor_type, actor_identifier, action, resource_type,
                resource_identifier, result, safe_cause_code, operation_id, correlation_id)
               VALUES (%s, 'PROCESS', %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING""",
            (event_id, process_identifier, action, resource_type, resource_identifier,
             result, safe_cause_code, operation_id, correlation_id),
        )
    except psycopg.Error:
        raise AuditPersistenceError("La auditoría del proceso no pudo confirmarse") from None
    return event_id
