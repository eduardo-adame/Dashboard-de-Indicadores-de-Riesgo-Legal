"""Persistencia durable de la ejecución proactiva programada."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row


class CoreDurableStateGap(RuntimeError):
    """El freeze proactivo no puede reconciliarse con un único run durable."""


class ProactiveIntegrationRepository:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                yield connection

    @contextmanager
    def execution_lock(self, operation_id: UUID) -> Iterator[None]:
        """Serializa una ejecución lógica sin bloquear filas usadas por CORE."""
        with psycopg.connect(self._conninfo) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (str(operation_id),),
                )
                yield

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
            "SELECT * FROM app.job_run WHERE operation_id=%s" + suffix,
            (operation_id,),
        ).fetchone()

    @staticmethod
    def create_job(
        connection,
        *,
        operation_id: UUID,
        correlation_id: UUID,
        actor_process: str,
        window_start: datetime,
        window_end: datetime,
        references: dict[str, object],
    ) -> UUID:
        job_id = uuid4()
        connection.execute(
            """INSERT INTO app.job_run
               (id, case_type, state, actor_process, window_start, window_end,
                result_references, safe_error_code, operation_id, correlation_id,
                completed_at)
               VALUES (%s, 'PROACTIVE_ANALYSIS', 'STARTED', %s, %s, %s,
                       %s::jsonb, NULL, %s, %s, NULL)""",
            (
                job_id,
                actor_process,
                window_start,
                window_end,
                json.dumps(references, ensure_ascii=False, sort_keys=True),
                operation_id,
                correlation_id,
            ),
        )
        return job_id

    @staticmethod
    def restart_job(connection, job_id: UUID) -> None:
        connection.execute(
            """UPDATE app.job_run
                  SET state='STARTED', safe_error_code=NULL, completed_at=NULL
                WHERE id=%s""",
            (job_id,),
        )

    @staticmethod
    def finalize_job(
        connection,
        job_id: UUID,
        *,
        state: str,
        references: dict[str, object],
        safe_error_code: str | None = None,
    ) -> None:
        connection.execute(
            """UPDATE app.job_run
                  SET state=%s,
                      result_references=%s::jsonb,
                      safe_error_code=%s,
                      completed_at=CASE
                          WHEN %s IN ('COMPLETED', 'FAILED', 'NO_RELEVANT_WORK')
                          THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE id=%s""",
            (
                state,
                json.dumps(references, ensure_ascii=False, sort_keys=True),
                safe_error_code,
                state,
                job_id,
            ),
        )

    @staticmethod
    def linked_core_run(connection, job) -> object | None:
        snapshot_count = connection.execute(
            "SELECT count(*) FROM app.proactive_input_snapshot WHERE job_run_id=%s",
            (job["id"],),
        ).fetchone()["count"]
        if snapshot_count == 0:
            return None

        rows = connection.execute(
            """SELECT DISTINCT ar.*
                 FROM app.proactive_input_snapshot snapshot
                 JOIN app.analytic_run ar ON ar.id=snapshot.analytic_run_id
                WHERE snapshot.job_run_id=%s""",
            (job["id"],),
        ).fetchall()
        if len(rows) != 1:
            raise CoreDurableStateGap("el freeze no está ligado a un único run")

        run = rows[0]
        rules_reference = run["rules_reference"] or {}
        if (
            run["run_type"] != "PROACTIVE_ANALYSIS"
            or run["correlation_id"] != job["correlation_id"]
            or rules_reference.get("parent_job_run_id") != str(job["id"])
            or rules_reference.get("parent_operation_id") != str(job["operation_id"])
        ):
            raise CoreDurableStateGap("el run no coincide con el job proactivo")
        return run
