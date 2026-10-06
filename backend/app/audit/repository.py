"""Lectura parametrizada y calificada del alcance autorizado de la bitácora."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.audit.models import AuditEventResponse, AuditPageResponse, AuditQuery, AuditUnavailableError


OWN_ACTIONS = (
    "INGESTION_COMPLETED", "INGESTION_DENIED", "VALIDATION_FILE_REJECTED",
    "QUARANTINE_CREATED", "QUARANTINE_REINJECT", "QUARANTINE_DISCARD", "QUARANTINE_VIEW",
)


class AuditRepository:
    def __init__(self, conninfo: str | None):
        self._conninfo = conninfo

    @contextmanager
    def read_transaction(self):
        if not self._conninfo:
            raise AuditUnavailableError("Consulta de auditoría no disponible")
        try:
            with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
                with connection.transaction():
                    connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute("SET LOCAL search_path = pg_catalog")
                    connection.execute("SET LOCAL statement_timeout = '5s'")
                    yield connection
        except psycopg.Error:
            raise AuditUnavailableError("Consulta de auditoría no disponible") from None

    @staticmethod
    def query_sql(query: AuditQuery, own_account_id: UUID | None):
        clauses, parameters = ["TRUE"], []
        if own_account_id is not None:
            clauses.append("e.actor_user_id = %s AND e.action = ANY(%s)")
            parameters.extend((own_account_id, list(OWN_ACTIONS)))
        for column, operator, value in (
            ("occurred_at", ">=", query.occurred_from), ("occurred_at", "<=", query.occurred_to),
            ("action", "=", query.action), ("resource_type", "=", query.resource_type),
            ("correlation_id", "=", query.correlation_id),
        ):
            if value is not None:
                clauses.append(f"e.{column} {operator} %s")
                parameters.append(value)
        cursor = query.decode_cursor(own_account_id)
        if cursor:
            clauses.append("(e.occurred_at, e.id) < (%s, %s)")
            parameters.extend(cursor)
        parameters.append(query.limit + 1)
        statement = """SELECT e.id, e.occurred_at, e.actor_type, e.actor_identifier,
            e.actor_user_id, e.action, e.resource_type, e.resource_identifier,
            e.result, e.safe_cause_code, e.operation_id, e.correlation_id, e.query_sha256
            FROM audit.event e WHERE """ + " AND ".join(clauses) + " ORDER BY e.occurred_at DESC, e.id DESC LIMIT %s"
        return statement, parameters

    def list_events(self, connection, query: AuditQuery, own_account_id: UUID | None) -> AuditPageResponse:
        statement, parameters = self.query_sql(query, own_account_id)
        rows = connection.execute(statement, parameters).fetchall()
        more, selected = len(rows) > query.limit, rows[:query.limit]
        resources = {}
        if selected:
            relations = connection.execute(
                """SELECT event_id, resource_type, resource_identifier, id_documento, fragment_id
                   FROM audit.event_resource WHERE event_id = ANY(%s)
                   ORDER BY event_id, resource_type, resource_identifier""",
                ([row["id"] for row in selected],),
            ).fetchall()
            for relation in relations:
                resources.setdefault(relation["event_id"], []).append(relation)
        items = []
        for row in selected:
            values = dict(row)
            values["query_sha256"] = bytes(row["query_sha256"]).hex() if row["query_sha256"] is not None else None
            values["resources"] = resources.get(row["id"], [])
            items.append(AuditEventResponse(**values))
        next_cursor = query.encode_cursor(items[-1].occurred_at, items[-1].id, own_account_id) if more else None
        return AuditPageResponse(items=items, next_cursor=next_cursor)
