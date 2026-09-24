"""Persistencia PostgreSQL para el núcleo de indicadores."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, time
import json
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.analytics.models import KpiObservationResult


class AnalyticsRepository:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                yield connection

    @staticmethod
    def acquire_run_lock(connection) -> None:
        """Serializa el alta de runs antes de tomar el snapshot consistente."""
        connection.execute("LOCK TABLE app.analytic_run IN SHARE ROW EXCLUSIVE MODE")

    @staticmethod
    def run_for_operation(connection, operation_id: UUID):
        return connection.execute("SELECT * FROM app.analytic_run WHERE operation_id = %s FOR UPDATE", (operation_id,)).fetchone()

    @staticmethod
    def create_run(connection, *, operation_id: UUID, correlation_id: UUID, codes: tuple[str, ...], period_start: date, period_end: date, as_of_date: date, parent_operation_id: UUID) -> UUID:
        run_id = uuid4()
        row = connection.execute(
            """INSERT INTO app.analytic_run
               (id, run_type, state, window_start, window_end, kpi_codes, rules_reference, result, operation_id, correlation_id)
               VALUES (%s, 'KPI_RECALCULATION', 'STARTED', %s, %s, %s, %s::jsonb, 'PENDING', %s, %s)
               ON CONFLICT (operation_id) DO NOTHING RETURNING id""",
            (run_id, _start(period_start), _end(period_end), list(codes), json.dumps({"as_of_date": as_of_date.isoformat(), "parent_operation_id": str(parent_operation_id)}), operation_id, correlation_id),
        ).fetchone()
        return None if row is None else run_id

    @staticmethod
    def restart_failed_run(connection, run_id: UUID) -> None:
        connection.execute("UPDATE app.analytic_run SET state='STARTED', result='PENDING', not_evaluated_reason=NULL, completed_at=NULL WHERE id=%s", (run_id,))

    @staticmethod
    def complete_run(connection, run_id: UUID) -> None:
        connection.execute("UPDATE app.analytic_run SET state='COMPLETED', result='SUCCESS', completed_at=CURRENT_TIMESTAMP WHERE id=%s", (run_id,))

    @staticmethod
    def fail_run(connection, run_id: UUID, safe_cause: str) -> None:
        connection.execute("UPDATE app.analytic_run SET state='FAILED', result='FAILURE', not_evaluated_reason=%s, completed_at=CURRENT_TIMESTAMP WHERE id=%s", (safe_cause, run_id))

    @staticmethod
    def upsert_observation(connection, observation: KpiObservationResult, run_id: UUID) -> None:
        dimensions = json.dumps(dict(observation.dimensions), ensure_ascii=False, sort_keys=True)
        connection.execute(
            """INSERT INTO app.kpi_observation
               (id, analytic_run_id, kpi_code, period_start, period_end, dimensions, value, availability, as_of_date)
               VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
               ON CONFLICT (kpi_code, period_start, period_end, dimensions) DO UPDATE
               SET analytic_run_id=EXCLUDED.analytic_run_id, value=EXCLUDED.value,
                   availability=EXCLUDED.availability, as_of_date=EXCLUDED.as_of_date,
                   calculated_at=CURRENT_TIMESTAMP
               WHERE EXCLUDED.as_of_date >= app.kpi_observation.as_of_date""",
            (uuid4(), run_id, observation.kpi_code, observation.period_start, observation.period_end, dimensions, observation.value, observation.availability, observation.as_of_date),
        )

    @staticmethod
    def current_observations(connection, *, codes: tuple[str, ...], period_start: date, period_end: date):
        return connection.execute(
            """SELECT kpi_code, period_start, period_end, dimensions, value, availability, as_of_date
                 FROM app.kpi_observation WHERE kpi_code = ANY(%s) AND period_start=%s AND period_end=%s
                 ORDER BY kpi_code, dimensions::text""", (list(codes), period_start, period_end),
        ).fetchall()

    @staticmethod
    def contract_rows(connection, period_start: date, period_end: date):
        return connection.execute("SELECT * FROM app.contract_record WHERE fecha_firma BETWEEN %s AND %s", (period_start, period_end)).fetchall()

    @staticmethod
    def contracts_snapshot(connection):
        return connection.execute("SELECT * FROM app.contract_record").fetchall()

    @staticmethod
    def litigation_rows(connection, period_start: date, period_end: date):
        return connection.execute("SELECT * FROM app.litigation WHERE fecha_apertura BETWEEN %s AND %s", (period_start, period_end)).fetchall()

    @staticmethod
    def active_litigations(connection):
        return connection.execute("SELECT * FROM app.litigation WHERE estado='Activo'").fetchall()

    @staticmethod
    def compliance_snapshot(connection):
        return connection.execute("SELECT * FROM app.compliance_obligation").fetchall()

    @staticmethod
    def incident_rows(connection, period_start: date, period_end: date):
        return connection.execute("SELECT * FROM app.incident WHERE fecha_evento BETWEEN %s AND %s", (period_start, period_end)).fetchall()

    @staticmethod
    def incident_dimensions(connection):
        return connection.execute("SELECT DISTINCT area, nivel_severidad FROM app.incident WHERE area IS NOT NULL AND nivel_severidad IS NOT NULL").fetchall()

    @staticmethod
    def matter_rows(connection, period_start: date, period_end: date):
        return connection.execute("SELECT * FROM app.legal_matter WHERE fecha BETWEEN %s AND %s", (period_start, period_end)).fetchall()

    @staticmethod
    def matter_dimensions(connection):
        return connection.execute("SELECT DISTINCT tipo_asunto, estado FROM app.legal_matter WHERE tipo_asunto IS NOT NULL AND estado IS NOT NULL").fetchall()

    @staticmethod
    def ocr_final_rows(connection):
        return connection.execute(
            """WITH selected AS (
                SELECT d.id_documento, r.confianza_agregada,
                       row_number() OVER (PARTITION BY d.id_documento ORDER BY r.processed_at DESC, v.version_number DESC, r.attempt_number DESC, r.id DESC) AS ordinal
                  FROM app.document d JOIN app.document_version v ON v.id_documento=d.id_documento
                  JOIN app.ocr_run r ON r.document_version_id=v.id
                 WHERE r.is_final AND r.estado_ocr IN ('Exitoso','Rechazado por baja confianza')
               ) SELECT id_documento, confianza_agregada FROM selected WHERE ordinal=1"""
        ).fetchall()

    @staticmethod
    def list_observations(connection, *, kpi_code: str | None, period_start: date | None, period_end: date | None):
        clauses, values = [], []
        if kpi_code is not None: clauses.append("kpi_code=%s"); values.append(kpi_code)
        if period_start is not None: clauses.append("period_start >= %s"); values.append(period_start)
        if period_end is not None: clauses.append("period_end <= %s"); values.append(period_end)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return connection.execute("SELECT * FROM app.kpi_observation" + where + " ORDER BY kpi_code, period_start, dimensions::text", tuple(values)).fetchall()


def _start(value: date) -> datetime: return datetime.combine(value, time.min, tzinfo=UTC)
def _end(value: date) -> datetime: return datetime.combine(value, time.max, tzinfo=UTC)
