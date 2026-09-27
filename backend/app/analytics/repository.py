"""Persistencia PostgreSQL para el núcleo de indicadores."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, time
import json
from typing import Iterator
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.analytics.models import (
    KpiObservationResult,
    ProactiveContextReferenceResult,
    ProactiveEvaluationResult,
    ProactiveFindingResult,
    ProactiveObservation,
)
from app.analytics.proactive_rules import canonical_dimensions, derive_snapshot_id


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

    @staticmethod
    def proactive_job(connection, job_run_id: UUID):
        return connection.execute(
            "SELECT * FROM app.job_run WHERE id=%s FOR UPDATE",
            (job_run_id,),
        ).fetchone()

    @staticmethod
    def eligible_proactive_observations(connection) -> tuple[ProactiveObservation, ...]:
        rows = connection.execute(
            """SELECT id, analytic_run_id, kpi_code, dimensions, period_start, period_end,
                      as_of_date, availability, value, calculated_at
                 FROM app.kpi_observation
                WHERE kpi_code = ANY(%s)
                ORDER BY kpi_code, dimensions::text, period_start, period_end, id""",
            (["KPI-RC-03", "KPI-LI-01", "KPI-LI-05", "KPI-CN-02", "KPI-CN-03", "KPI-RC-01", "KPI-EO-01"],),
        ).fetchall()
        return tuple(
            ProactiveObservation(
                row["id"], row["analytic_run_id"], row["kpi_code"], row["dimensions"],
                row["period_start"], row["period_end"], row["as_of_date"],
                row["availability"], row["value"], row["calculated_at"],
            )
            for row in rows
        )

    @staticmethod
    def latest_completed_proactive_run(connection):
        return connection.execute(
            """SELECT * FROM app.analytic_run
                WHERE run_type='PROACTIVE_ANALYSIS' AND state='COMPLETED'
                ORDER BY completed_at DESC, id DESC LIMIT 1"""
        ).fetchone()

    @staticmethod
    def proactive_run_for_job(connection, job_run_id: UUID, *, for_update: bool = False):
        lock = " FOR UPDATE" if for_update else ""
        return connection.execute(
            """SELECT * FROM app.analytic_run
                WHERE run_type='PROACTIVE_ANALYSIS'
                  AND rules_reference->>'parent_job_run_id'=%s
                ORDER BY started_at DESC, id DESC LIMIT 1""" + lock,
            (str(job_run_id),),
        ).fetchone()

    @staticmethod
    def create_proactive_run(
        connection,
        *,
        run_id: UUID,
        operation_id: UUID,
        correlation_id: UUID,
        window_start: datetime,
        window_end: datetime,
        codes: tuple[str, ...],
        rules_reference: dict,
    ) -> UUID | None:
        row = connection.execute(
            """INSERT INTO app.analytic_run
               (id, run_type, state, window_start, window_end, kpi_codes,
                dimensions, rules_reference, result, not_evaluated_reason,
                executive_summary, operation_id, correlation_id)
               VALUES (%s, 'PROACTIVE_ANALYSIS', 'STARTED', %s, %s, %s,
                       '{}'::jsonb, %s::jsonb, 'PENDING', NULL, NULL, %s, %s)
               ON CONFLICT (operation_id) DO NOTHING
               RETURNING id""",
            (run_id, window_start, window_end, list(codes), json.dumps(rules_reference, ensure_ascii=False, sort_keys=True), operation_id, correlation_id),
        ).fetchone()
        return None if row is None else row["id"]

    @staticmethod
    def persist_proactive_snapshot(connection, *, run_id: UUID, job_run_id: UUID, observations: tuple[ProactiveObservation, ...]) -> None:
        for row in observations:
            connection.execute(
                """INSERT INTO app.proactive_input_snapshot
                   (id, analytic_run_id, job_run_id, source_observation_id,
                    source_analytic_run_id, kpi_code, canonical_dimensions_key,
                    period_start, period_end, as_of_date, availability, value, calculated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (job_run_id, source_observation_id) DO NOTHING""",
                (
                    derive_snapshot_id(run_id, row.source_observation_id), run_id, job_run_id,
                    row.source_observation_id, row.source_analytic_run_id, row.kpi_code,
                    json.dumps(canonical_dimensions(row.canonical_dimensions_key), ensure_ascii=False, sort_keys=True),
                    row.period_start, row.period_end, row.as_of_date, row.availability,
                    row.value, row.calculated_at,
                ),
            )

    @staticmethod
    def proactive_snapshot(connection, run_id: UUID) -> tuple[ProactiveObservation, ...]:
        rows = connection.execute(
            """SELECT source_observation_id, source_analytic_run_id, kpi_code,
                      canonical_dimensions_key, period_start, period_end, as_of_date,
                      availability, value, calculated_at
                 FROM app.proactive_input_snapshot
                WHERE analytic_run_id=%s
                ORDER BY kpi_code, canonical_dimensions_key::text, period_start, source_observation_id""",
            (run_id,),
        ).fetchall()
        return tuple(
            ProactiveObservation(
                row["source_observation_id"], row["source_analytic_run_id"], row["kpi_code"],
                row["canonical_dimensions_key"], row["period_start"], row["period_end"],
                row["as_of_date"], row["availability"], row["value"], row["calculated_at"],
            )
            for row in rows
        )

    @staticmethod
    def persist_proactive_evaluation(connection, evaluation: ProactiveEvaluationResult) -> None:
        connection.execute(
            """INSERT INTO app.proactive_evaluation
               (id, analytic_run_id, kpi_code, canonical_dimensions_key,
                evaluation_period, outcome, signal_detected, trend_direction,
                recurrence_month_count, current_value, reference_value,
                absolute_variation, percentage_variation, rules_applied,
                not_evaluated_reason)
               VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (analytic_run_id, kpi_code, canonical_dimensions_key, evaluation_period)
               DO NOTHING""",
            (
                evaluation.id, evaluation.analytic_run_id, evaluation.kpi_code,
                json.dumps(canonical_dimensions(evaluation.canonical_dimensions_key), ensure_ascii=False, sort_keys=True),
                evaluation.evaluation_period, evaluation.outcome, evaluation.signal_detected,
                evaluation.trend_direction, evaluation.recurrence_month_count,
                evaluation.current_value, evaluation.reference_value,
                evaluation.absolute_variation, evaluation.percentage_variation,
                list(evaluation.rules_applied), evaluation.not_evaluated_reason,
            ),
        )

    @staticmethod
    def persist_proactive_finding(connection, finding: ProactiveFindingResult) -> None:
        connection.execute(
            """INSERT INTO app.finding
               (id, analytic_run_id, finding_type, description, kpi_code, dimensions,
                period_start, period_end, current_value, reference_value, variation,
                triggered_rule, recurrent_pattern, state, proactive_evaluation_id)
               VALUES (%s,%s,'PROACTIVE_SIGNAL',%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)
               ON CONFLICT (proactive_evaluation_id) WHERE proactive_evaluation_id IS NOT NULL
               DO NOTHING""",
            (
                finding.id, finding.analytic_run_id, finding.description, finding.kpi_code,
                json.dumps(canonical_dimensions(finding.dimensions), ensure_ascii=False, sort_keys=True),
                finding.period_start, finding.period_end, finding.current_value,
                finding.reference_value, finding.variation, finding.triggered_rule,
                finding.recurrent_pattern, finding.proactive_evaluation_id,
            ),
        )

    @staticmethod
    def persist_context_reference(connection, reference: ProactiveContextReferenceResult) -> None:
        connection.execute(
            """INSERT INTO app.context_reference
               (id, finding_id, kpi_code, dimensions, period_start, period_end,
                context_identifiers, operation_id, correlation_id)
               VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s)
               ON CONFLICT (finding_id) DO NOTHING""",
            (
                reference.id, reference.finding_id, reference.kpi_code,
                json.dumps(canonical_dimensions(reference.dimensions), ensure_ascii=False, sort_keys=True),
                reference.period_start, reference.period_end,
                json.dumps(dict(reference.context_identifiers), ensure_ascii=False, sort_keys=True),
                reference.operation_id, reference.correlation_id,
            ),
        )

    @staticmethod
    def complete_proactive_run(connection, run_id: UUID, executive_summary: str) -> None:
        connection.execute(
            """UPDATE app.analytic_run
                  SET state='COMPLETED', result='SUCCESS', not_evaluated_reason=NULL,
                      executive_summary=%s, completed_at=CURRENT_TIMESTAMP
                WHERE id=%s""",
            (executive_summary, run_id),
        )

    @staticmethod
    def proactive_evaluations(connection, run_id: UUID):
        return connection.execute(
            "SELECT * FROM app.proactive_evaluation WHERE analytic_run_id=%s ORDER BY kpi_code, canonical_dimensions_key::text, evaluation_period",
            (run_id,),
        ).fetchall()

    @staticmethod
    def proactive_findings(connection, run_id: UUID):
        return connection.execute(
            "SELECT * FROM app.finding WHERE analytic_run_id=%s AND proactive_evaluation_id IS NOT NULL ORDER BY kpi_code, dimensions::text, period_start, id",
            (run_id,),
        ).fetchall()

    @staticmethod
    def proactive_context_references(connection, run_id: UUID):
        return connection.execute(
            """SELECT c.* FROM app.context_reference c
                JOIN app.finding f ON f.id=c.finding_id
                WHERE f.analytic_run_id=%s
                ORDER BY c.kpi_code, c.dimensions::text, c.period_start, c.id""",
            (run_id,),
        ).fetchall()

    @staticmethod
    def list_completed_proactive_runs(connection, *, run_id: UUID | None = None, period_start: date | None = None, period_end: date | None = None):
        clauses = ["run_type='PROACTIVE_ANALYSIS'", "state='COMPLETED'"]
        values: list[object] = []
        if run_id is not None:
            clauses.append("id=%s"); values.append(run_id)
        if period_start is not None:
            clauses.append("window_end >= %s"); values.append(_start(period_start))
        if period_end is not None:
            clauses.append("window_start <= %s"); values.append(_end(period_end))
        return connection.execute(
            "SELECT * FROM app.analytic_run WHERE " + " AND ".join(clauses) + " ORDER BY completed_at DESC, id DESC",
            tuple(values),
        ).fetchall()


def _start(value: date) -> datetime: return datetime.combine(value, time.min, tzinfo=UTC)
def _end(value: date) -> datetime: return datetime.combine(value, time.max, tzinfo=UTC)
