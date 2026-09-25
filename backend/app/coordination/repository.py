"""Persistencia transaccional del despacho de coordinación.

Identidad idempotente: ``(file_id, downstream_target)``. ``operation_id`` se
genera una sola vez y se propaga como identidad funcional estable de la
operación downstream. ``correlation_id`` es solo trazabilidad.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.coordination.models import CoordinationDispatch, DispatchState, RoutingTarget

_CSV_XLSX = ("CSV", "XLSX")
_PDF_DOCX = ("PDF", "DOCX")


class CoordinationRepository:
    def __init__(self, conninfo: str, security_repository) -> None:
        self._conninfo = conninfo
        self._security_repository = security_repository

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    # -- reconstrucción autoritativa del destino ----------------------------
    @staticmethod
    def reconstruct_routing_target(connection: psycopg.Connection, file_id: UUID) -> RoutingTarget | None:
        row = connection.execute(
            "SELECT state, exchange_format FROM app.ingest_file WHERE id = %s",
            (file_id,),
        ).fetchone()
        if row is None:
            return None
        state = str(row["state"])
        exchange_format = row["exchange_format"]
        if state == "CUARENTENA":
            return RoutingTarget.VALIDATION
        if state == "COMPLETADO":
            if exchange_format in _CSV_XLSX:
                return RoutingTarget.VALIDATION
            if exchange_format in _PDF_DOCX:
                return RoutingTarget.DOCUMENT
        return None

    # -- ciclo de vida ------------------------------------------------------
    @staticmethod
    def get_or_create_dispatch(
        connection: psycopg.Connection,
        *,
        file_id: UUID,
        downstream_target: str,
        correlation_id: UUID,
    ) -> CoordinationDispatch:
        row = connection.execute(
            """INSERT INTO app.coordination_dispatch
               (id, operation_id, file_id, downstream_target, state, correlation_id)
               VALUES (gen_random_uuid(), gen_random_uuid(), %s, %s, 'NEW', %s)
               ON CONFLICT (file_id, downstream_target)
               DO UPDATE SET correlation_id = app.coordination_dispatch.correlation_id
               RETURNING id, operation_id, file_id, downstream_target, state, attempt_count,
                         correlation_id, downstream_result_id, safe_cause_code,
                         created_at, started_at, completed_at, heartbeat_at""",
            (file_id, downstream_target, correlation_id),
        ).fetchone()
        return _row_to_dispatch(row)

    @staticmethod
    def get(connection: psycopg.Connection, dispatch_id: UUID) -> CoordinationDispatch | None:
        row = connection.execute(
            """SELECT id, operation_id, file_id, downstream_target, state, attempt_count,
                      correlation_id, downstream_result_id, safe_cause_code,
                      created_at, started_at, completed_at, heartbeat_at
                 FROM app.coordination_dispatch WHERE id = %s""",
            (dispatch_id,),
        ).fetchone()
        return _row_to_dispatch(row) if row else None

    @staticmethod
    def claim(connection: psycopg.Connection, dispatch_id: UUID) -> bool:
        row = connection.execute(
            """UPDATE app.coordination_dispatch
                  SET state = 'IN_PROGRESS',
                      started_at = CURRENT_TIMESTAMP,
                      heartbeat_at = CURRENT_TIMESTAMP,
                      attempt_count = attempt_count + 1
                WHERE id = %s AND state IN ('NEW', 'FAILED')
                RETURNING id""",
            (dispatch_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def heartbeat(connection: psycopg.Connection, dispatch_id: UUID) -> None:
        connection.execute(
            "UPDATE app.coordination_dispatch SET heartbeat_at = CURRENT_TIMESTAMP WHERE id = %s AND state = 'IN_PROGRESS'",
            (dispatch_id,),
        )

    @staticmethod
    def complete(connection: psycopg.Connection, dispatch_id: UUID, downstream_result_id: UUID | None) -> None:
        connection.execute(
            """UPDATE app.coordination_dispatch
                  SET state = 'COMPLETED', completed_at = CURRENT_TIMESTAMP, downstream_result_id = %s
                WHERE id = %s AND state = 'IN_PROGRESS'""",
            (downstream_result_id, dispatch_id),
        )

    @staticmethod
    def fail(connection: psycopg.Connection, dispatch_id: UUID, safe_cause_code: str) -> None:
        connection.execute(
            """UPDATE app.coordination_dispatch
                  SET state = 'FAILED', completed_at = CURRENT_TIMESTAMP, safe_cause_code = %s
                WHERE id = %s AND state = 'IN_PROGRESS'""",
            (safe_cause_code, dispatch_id),
        )

    @staticmethod
    def recover_stale(connection: psycopg.Connection, stale_threshold_seconds: int) -> list[UUID]:
        rows = connection.execute(
            """UPDATE app.coordination_dispatch
                  SET state = 'FAILED', completed_at = CURRENT_TIMESTAMP, safe_cause_code = 'STALE_IN_PROGRESS'
                WHERE state = 'IN_PROGRESS'
                  AND heartbeat_at < CURRENT_TIMESTAMP - make_interval(secs => %s)
                RETURNING id""",
            (stale_threshold_seconds,),
        ).fetchall()
        return [row["id"] for row in rows]

    def write_audit_event(self, connection: psycopg.Connection, **kwargs) -> None:
        self._security_repository.write_audit_event(connection, **kwargs)


def _row_to_dispatch(row: dict[str, object]) -> CoordinationDispatch:
    return CoordinationDispatch(
        id=row["id"],
        operation_id=row["operation_id"],
        file_id=row["file_id"],
        downstream_target=str(row["downstream_target"]),
        state=DispatchState(str(row["state"])),
        attempt_count=int(row["attempt_count"]),
        correlation_id=row["correlation_id"],
        downstream_result_id=row["downstream_result_id"],
        safe_cause_code=row["safe_cause_code"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        heartbeat_at=row["heartbeat_at"],
    )
