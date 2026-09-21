"""Persistencia transaccional del módulo de validación y cuarentena.

La escritura de auditoría se delega en el repositorio de seguridad vigente para
preservar la semántica de la frontera de auditoría; el módulo de validación no
redefine esa política.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.ingestion.models import SourceFamily
from app.validation.models import QuarantineCause, QuarantineState
from app.validation.quarantine import QuarantineItem, QuarantineTransition


class ValidationRepository:
    def __init__(self, conninfo: str, security_repository) -> None:
        self._conninfo = conninfo
        self._security_repository = security_repository

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    # -- cuarentena ---------------------------------------------------------
    @staticmethod
    def find_quarantine_by_operation(connection: psycopg.Connection, operation_id: UUID) -> QuarantineItem | None:
        row = connection.execute(
            """SELECT id, ingest_file_id, source_record_id, cause_code, state,
                      original_payload, candidate_payload, discard_justification,
                      operation_id, correlation_id, created_at
                 FROM app.quarantine_item
                WHERE operation_id = %s""",
            (operation_id,),
        ).fetchone()
        return _row_to_item(row) if row else None

    @staticmethod
    def insert_quarantine(
        connection: psycopg.Connection,
        *,
        file_id: UUID,
        source_record_id: UUID | None,
        cause: QuarantineCause | None,
        operation_id: UUID,
        correlation_id: UUID,
        original_payload: dict[str, object] | None,
    ) -> UUID:
        row = connection.execute(
            """INSERT INTO app.quarantine_item
               (id, ingest_file_id, source_record_id, cause_code, state,
                original_payload, operation_id, correlation_id)
               VALUES (gen_random_uuid(), %s, %s, %s, 'Pendiente', %s::jsonb, %s, %s)
               RETURNING id""",
            (
                file_id,
                source_record_id,
                (cause or QuarantineCause.OTHER_CAUSE).value,
                json.dumps(original_payload, ensure_ascii=False, default=str) if original_payload else None,
                operation_id,
                correlation_id,
            ),
        ).fetchone()
        return row["id"]

    @staticmethod
    def load_quarantine(connection: psycopg.Connection, item_id: UUID, *, for_update: bool = False) -> QuarantineItem | None:
        suffix = " FOR UPDATE" if for_update else ""
        row = connection.execute(
            """SELECT id, ingest_file_id, source_record_id, cause_code, state,
                      original_payload, candidate_payload, discard_justification,
                      operation_id, correlation_id, created_at
                 FROM app.quarantine_item
                WHERE id = %s""" + suffix,
            (item_id,),
        ).fetchone()
        return _row_to_item(row) if row else None

    @staticmethod
    def update_quarantine(connection: psycopg.Connection, item: QuarantineItem) -> None:
        connection.execute(
            """UPDATE app.quarantine_item
                  SET cause_code = %s,
                      state = %s,
                      candidate_payload = %s::jsonb,
                      discard_justification = %s,
                      updated_at = CURRENT_TIMESTAMP
                WHERE id = %s""",
            (
                item.cause.value,
                item.state.value,
                json.dumps(item.candidate_payload, ensure_ascii=False, default=str) if item.candidate_payload else None,
                item.discard_justification,
                item.id,
            ),
        )

    @staticmethod
    def insert_transition(connection: psycopg.Connection, transition: QuarantineTransition) -> None:
        connection.execute(
            """INSERT INTO app.quarantine_transition
               (id, quarantine_item_id, from_state, to_state, actor_type, actor_identifier,
                operation_id, correlation_id)
               VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s)""",
            (
                transition.item_id,
                transition.from_state.value,
                transition.to_state.value,
                transition.actor_type,
                transition.actor_identifier,
                transition.operation_id,
                transition.correlation_id,
            ),
        )

    @staticmethod
    def family_for_quarantine(connection: psycopg.Connection, item: QuarantineItem) -> SourceFamily:
        if item.ingest_file_id is None:
            return SourceFamily.CONTRACTS_DOCUMENTS
        row = connection.execute(
            "SELECT source_family FROM app.ingest_file WHERE id = %s",
            (item.ingest_file_id,),
        ).fetchone()
        if row is None:
            return SourceFamily.CONTRACTS_DOCUMENTS
        return SourceFamily(str(row["source_family"]))

    # -- auditoría ----------------------------------------------------------
    def write_audit_event(self, connection, **kwargs) -> None:
        """Delega en la frontera de auditoría vigente."""
        self._security_repository.write_audit_event(connection, **kwargs)

    # -- consulta -----------------------------------------------------------
    @staticmethod
    def list_quarantine(
        connection: psycopg.Connection,
        *,
        state: str | None = None,
        ingest_file_id: UUID | None = None,
        cause: str | None = None,
    ) -> list[QuarantineItem]:
        clauses = []
        params: dict[str, object] = {}
        if state is not None:
            clauses.append("state = %(state)s")
            params["state"] = state
        if ingest_file_id is not None:
            clauses.append("ingest_file_id = %(file)s")
            params["file"] = ingest_file_id
        if cause is not None:
            clauses.append("cause_code = %(cause)s")
            params["cause"] = cause
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"""SELECT id, ingest_file_id, source_record_id, cause_code, state,
                       original_payload, candidate_payload, discard_justification,
                       operation_id, correlation_id, created_at
                  FROM app.quarantine_item
                  {where}
                 ORDER BY created_at""",
            params,
        ).fetchall()
        return [_row_to_item(row) for row in rows]


def _row_to_item(row: dict[str, object]) -> QuarantineItem:
    return QuarantineItem(
        id=row["id"],
        ingest_file_id=row["ingest_file_id"],
        source_record_id=row["source_record_id"],
        cause=QuarantineCause(str(row["cause_code"])),
        state=QuarantineState(str(row["state"])),
        operation_id=row["operation_id"],
        correlation_id=row["correlation_id"],
        original_payload=row["original_payload"],
        candidate_payload=row["candidate_payload"],
        discard_justification=row["discard_justification"],
        created_at=row["created_at"],
    )
