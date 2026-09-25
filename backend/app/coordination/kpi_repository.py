"""Persistencia durable de solicitudes de recálculo coordinadas."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, time
import json
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


class KpiIntegrationRepository:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    @staticmethod
    def lock_operation(connection, operation_id: UUID) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (str(operation_id),),
        )

    @staticmethod
    def job_for_operation(connection, operation_id: UUID, *, for_update: bool = False):
        suffix = " FOR UPDATE" if for_update else ""
        return connection.execute(
            "SELECT * FROM app.job_run WHERE operation_id = %s" + suffix,
            (operation_id,),
        ).fetchone()

    @staticmethod
    def create_job(
        connection,
        *,
        operation_id: UUID,
        correlation_id: UUID,
        actor_process: str,
        window_start: date | None,
        window_end: date | None,
        references: dict[str, object],
        state: str = "STARTED",
    ) -> UUID:
        job_id = uuid4()
        connection.execute(
            """INSERT INTO app.job_run
               (id, case_type, state, actor_process, window_start, window_end,
                result_references, operation_id, correlation_id, completed_at)
               VALUES (%s, 'KPI_RECALCULATION', %s, %s, %s, %s, %s::jsonb, %s, %s,
                       CASE WHEN %s IN ('COMPLETED', 'FAILED', 'NO_RELEVANT_WORK')
                            THEN CURRENT_TIMESTAMP ELSE NULL END)""",
            (
                job_id, state, actor_process, _start(window_start), _end(window_end),
                json.dumps(references, ensure_ascii=False, sort_keys=True), operation_id,
                correlation_id, state,
            ),
        )
        return job_id

    @staticmethod
    def update_job(connection, job_id: UUID, *, state: str, references: dict[str, object], safe_error_code: str | None = None) -> None:
        connection.execute(
            """UPDATE app.job_run
                  SET state = %s, result_references = %s::jsonb, safe_error_code = %s,
                      completed_at = CASE WHEN %s IN ('COMPLETED', 'FAILED', 'NO_RELEVANT_WORK')
                                          THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE id = %s""",
            (state, json.dumps(references, ensure_ascii=False, sort_keys=True), safe_error_code, state, job_id),
        )

    @staticmethod
    def projection_reference(connection, *, source_record_id: UUID, entity_type: str, business_id: str):
        return connection.execute(
            """SELECT applied_at FROM app.record_application
                 WHERE source_record_id=%s AND entity_type=%s AND business_id=%s""",
            (source_record_id, entity_type, business_id),
        ).fetchone()

    @staticmethod
    def business_temporal_value(connection, *, entity_type: str, business_id: str):
        tables = {
            "CONTRATO": ("contract_record", "id_contrato", "fecha_firma"),
            "LITIGIO": ("litigation", "id_litigio", "fecha_apertura"),
            "INCIDENTE": ("incident", "id_incidente", "fecha_evento"),
            "ASUNTO": ("legal_matter", "id_asunto", "fecha"),
        }
        match = tables.get(entity_type)
        if match is None:
            return None
        table, identifier, column = match
        row = connection.execute(
            f"SELECT {column} AS value FROM app.{table} WHERE {identifier}=%s",
            (business_id,),
        ).fetchone()
        return None if row is None else row["value"]

    @staticmethod
    def final_ocr_for_operation(connection, operation_id: UUID):
        return connection.execute(
            """SELECT id, processed_at, estado_ocr, is_final
                 FROM app.ocr_run WHERE operation_id=%s
                 ORDER BY processed_at DESC, id DESC LIMIT 1""",
            (operation_id,),
        ).fetchone()


def _start(value: date | None) -> datetime | None:
    return None if value is None else datetime.combine(value, time.min, tzinfo=UTC)


def _end(value: date | None) -> datetime | None:
    return None if value is None else datetime.combine(value, time.max, tzinfo=UTC)
