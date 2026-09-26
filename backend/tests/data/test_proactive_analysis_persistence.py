"""Contratos PostgreSQL de la persistencia del análisis proactivo."""
from __future__ import annotations

import os
from pathlib import Path
import uuid

from alembic import command
from alembic.config import Config
import psycopg
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _test_url() -> str:
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("DATA_TEST_DATABASE_URL no configurada")
    if "test" not in (make_url(url).database or "").lower():
        pytest.fail("DATA_TEST_DATABASE_URL debe apuntar a una base desechable de prueba")
    return url


def _config(url: str) -> Config:
    parsed = make_url(url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost",
        POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "",
        POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    return config


@pytest.fixture()
def proactive_database() -> tuple[sa.Engine, Config]:
    url = _test_url()
    config = _config(url)
    engine = sa.create_engine(url)
    command.upgrade(config, "0013_proactive_analysis")
    _clear_proactive_evidence(engine)
    try:
        yield engine, config
    finally:
        command.upgrade(config, "0013_proactive_analysis")
        _clear_proactive_evidence(engine)
        engine.dispose()


def _clear_proactive_evidence(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE app.finding SET proactive_evaluation_id = NULL WHERE proactive_evaluation_id IS NOT NULL"))
        connection.execute(
            sa.text(
                "ALTER TABLE app.proactive_input_snapshot "
                "DISABLE TRIGGER trg_proactive_snapshot_immutable"
            )
        )
        try:
            connection.execute(sa.text("DELETE FROM app.proactive_input_snapshot"))
        finally:
            connection.execute(
                sa.text(
                    "ALTER TABLE app.proactive_input_snapshot "
                    "ENABLE TRIGGER trg_proactive_snapshot_immutable"
                )
            )
        connection.execute(sa.text("DELETE FROM app.proactive_evaluation"))


def _insert_run(connection, *, run_type: str = "PROACTIVE_ANALYSIS") -> uuid.UUID:
    run_id = uuid.uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO app.analytic_run "
            "(id, run_type, state, result, operation_id, correlation_id, completed_at) "
            "VALUES (:id, :type, 'COMPLETED', 'SUCCESS', :operation, :correlation, CURRENT_TIMESTAMP)"
        ),
        {"id": run_id, "type": run_type, "operation": uuid.uuid4(), "correlation": uuid.uuid4()},
    )
    return run_id


def _insert_job(
    connection,
    *,
    case_type: str = "PROACTIVE_ANALYSIS",
    state: str = "COMPLETED",
) -> uuid.UUID:
    job_id = uuid.uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO app.job_run "
            "(id, case_type, state, actor_process, operation_id, correlation_id, completed_at) "
            "VALUES (:id, :case_type, :state, 'test', :operation, :correlation, CURRENT_TIMESTAMP)"
        ),
        {
            "id": job_id,
            "case_type": case_type,
            "state": state,
            "operation": uuid.uuid4(),
            "correlation": uuid.uuid4(),
        },
    )
    return job_id


def _insert_source_observation(connection, *, code: str = "KPI-RC-03") -> tuple[uuid.UUID, uuid.UUID]:
    source_run_id = _insert_run(connection, run_type="KPI_RECALCULATION")
    observation_id = uuid.uuid4()
    dimensions = '{"test_series": "' + str(uuid.uuid4()) + '"}'
    connection.execute(
        sa.text(
            "INSERT INTO app.kpi_observation "
            "(id, analytic_run_id, kpi_code, period_start, period_end, dimensions, value, availability, as_of_date) "
            "VALUES (:id, :run_id, :code, '2030-01-01', '2030-01-31', CAST(:dimensions AS jsonb), "
            "10, 'DISPONIBLE', '2030-01-31')"
        ),
        {"id": observation_id, "run_id": source_run_id, "code": code, "dimensions": dimensions},
    )
    return observation_id, source_run_id


def _snapshot_statement() -> sa.TextClause:
    return sa.text(
        "INSERT INTO app.proactive_input_snapshot "
        "(id, analytic_run_id, job_run_id, source_observation_id, source_analytic_run_id, kpi_code, "
        "canonical_dimensions_key, period_start, period_end, as_of_date, availability, value, calculated_at) "
        "VALUES (:id, :analytic_run_id, :job_run_id, :source_observation_id, :source_analytic_run_id, "
        ":kpi_code, CAST(:dimensions AS jsonb), :period_start, :period_end, :as_of_date, :availability, :value, CURRENT_TIMESTAMP)"
    )


def _snapshot_params(target_run: uuid.UUID, job_id: uuid.UUID, source_observation: uuid.UUID, source_run: uuid.UUID, **overrides) -> dict:
    params = {
        "id": uuid.uuid4(), "analytic_run_id": target_run, "job_run_id": job_id,
        "source_observation_id": source_observation, "source_analytic_run_id": source_run,
        "kpi_code": "KPI-RC-03", "dimensions": "{}", "period_start": "2030-01-01",
        "period_end": "2030-01-31", "as_of_date": "2030-01-31", "availability": "DISPONIBLE", "value": 10,
    }
    params.update(overrides)
    return params


def _evaluation_statement() -> sa.TextClause:
    return sa.text(
        "INSERT INTO app.proactive_evaluation "
        "(id, analytic_run_id, kpi_code, canonical_dimensions_key, evaluation_period, outcome, signal_detected, "
        "trend_direction, recurrence_month_count, current_value, rules_applied, not_evaluated_reason) "
        "VALUES (:id, :analytic_run_id, :kpi_code, CAST(:dimensions AS jsonb), :evaluation_period, :outcome, "
        ":signal_detected, :trend_direction, :recurrence_month_count, :current_value, :rules_applied, :reason)"
    )


def _evaluation_params(run_id: uuid.UUID, **overrides) -> dict:
    params = {
        "id": uuid.uuid4(), "analytic_run_id": run_id, "kpi_code": "KPI-RC-03", "dimensions": "{}",
        "evaluation_period": "2030-01-01", "outcome": "EVALUATED", "signal_detected": False,
        "trend_direction": None, "recurrence_month_count": 0, "current_value": 10,
        "rules_applied": ["RC03_APPEARANCE_OR_INCREASE"], "reason": None,
    }
    params.update(overrides)
    return params


def _assert_integrity(connection, statement, params, constraint: str) -> None:
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(IntegrityError) as exc_info:
            connection.execute(statement, params)
        assert isinstance(exc_info.value.orig, psycopg.errors.CheckViolation | psycopg.errors.UniqueViolation | psycopg.errors.ForeignKeyViolation)
        assert exc_info.value.orig.diag.constraint_name == constraint
    finally:
        savepoint.rollback()


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_upgrade_0012_to_0013(proactive_database) -> None:
    engine, config = proactive_database
    command.downgrade(config, "0012_kpi_observation_semantics")
    command.upgrade(config, "0013_proactive_analysis")
    tables = set(sa.inspect(engine).get_table_names(schema="app"))
    assert {"proactive_input_snapshot", "proactive_evaluation"}.issubset(tables)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_legacy_rows_survive_upgrade(proactive_database) -> None:
    engine, config = proactive_database
    command.downgrade(config, "0012_kpi_observation_semantics")
    with engine.begin() as connection:
        run_id = _insert_run(connection)
        finding_id = uuid.uuid4()
        connection.execute(sa.text("INSERT INTO app.finding (id, analytic_run_id, finding_type, description, triggered_rule, state) VALUES (:id, :run, 'SIGNAL', 'legacy', 'LEGACY', 'OPEN')"), {"id": finding_id, "run": run_id})
    command.upgrade(config, "0013_proactive_analysis")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT proactive_evaluation_id FROM app.finding WHERE id = :id"), {"id": finding_id}).scalar_one() is None
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM app.finding WHERE id = :id"), {"id": finding_id})
        connection.execute(sa.text("DELETE FROM app.analytic_run WHERE id = :id"), {"id": run_id})


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_legacy_findings_keep_null_evaluation(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.finding WHERE proactive_evaluation_id IS NOT NULL")).scalar_one() == 0


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_month_constraints(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        _assert_integrity(
            connection,
            _snapshot_statement(),
            _snapshot_params(
                target,
                job,
                observation,
                source,
                period_start="2030-01-02",
                period_end="2030-02-01",
            ),
            "ck_proactive_snapshot_period_start_monthly",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_dimensions_must_be_object(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        _assert_integrity(connection, _snapshot_statement(), _snapshot_params(target, job, observation, source, dimensions="[]"), "ck_proactive_snapshot_dimensions_object")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_availability_value_consistency(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        _assert_integrity(connection, _snapshot_statement(), _snapshot_params(target, job, observation, source, availability="NO_DISPONIBLE", value=10), "ck_proactive_snapshot_availability")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_kpi_check(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        connection.execute(_snapshot_statement(), _snapshot_params(target, job, observation, source, kpi_code="KPI-RC-01"))
        _assert_integrity(connection, _snapshot_statement(), _snapshot_params(target, job, observation, source, kpi_code="KPI-CD-03"), "ck_proactive_snapshot_kpi_code")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_job_source_uniqueness(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        params = _snapshot_params(target, job, observation, source)
        connection.execute(_snapshot_statement(), params)
        _assert_integrity(connection, _snapshot_statement(), _snapshot_params(target, job, observation, source), "uq_proactive_snapshot_job_source")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_rejects_no_relevant_work_job(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target = _insert_run(connection)
        job = _insert_job(connection, state="NO_RELEVANT_WORK")
        observation, source = _insert_source_observation(connection)
        _assert_integrity(
            connection,
            _snapshot_statement(),
            _snapshot_params(target, job, observation, source),
            "ck_proactive_snapshot_job_state",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_job_cannot_transition_to_no_relevant_work_with_snapshots(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target = _insert_run(connection)
        job = _insert_job(connection, state="STARTED")
        observation, source = _insert_source_observation(connection)
        connection.execute(
            _snapshot_statement(),
            _snapshot_params(target, job, observation, source),
        )
        _assert_integrity(
            connection,
            sa.text("UPDATE app.job_run SET state = 'NO_RELEVANT_WORK' WHERE id = :id"),
            {"id": job},
            "ck_job_run_no_relevant_work_without_snapshots",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_relevant_proactive_job_accepts_snapshots(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target = _insert_run(connection)
        job = _insert_job(connection, state="STARTED")
        observation, source = _insert_source_observation(connection)
        snapshot = _snapshot_params(target, job, observation, source)
        connection.execute(_snapshot_statement(), snapshot)
        assert connection.execute(
            sa.text("SELECT count(*) FROM app.proactive_input_snapshot WHERE id = :id"),
            {"id": snapshot["id"]},
        ).scalar_one() == 1


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_rejects_non_proactive_job(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target = _insert_run(connection)
        job = _insert_job(connection, case_type="KPI_RECALCULATION")
        observation, source = _insert_source_observation(connection)
        _assert_integrity(
            connection,
            _snapshot_statement(),
            _snapshot_params(target, job, observation, source),
            "ck_proactive_snapshot_job_case",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_source_observation_run_match_is_accepted(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        snapshot = _snapshot_params(target, job, observation, source)
        connection.execute(_snapshot_statement(), snapshot)
        assert connection.execute(
            sa.text("SELECT source_analytic_run_id FROM app.proactive_input_snapshot WHERE id = :id"),
            {"id": snapshot["id"]},
        ).scalar_one() == source


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_source_observation_run_mismatch_is_rejected(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, _ = _insert_source_observation(connection)
        wrong_source_run = _insert_run(connection, run_type="KPI_RECALCULATION")
        _assert_integrity(
            connection,
            _snapshot_statement(),
            _snapshot_params(target, job, observation, wrong_source_run),
            "fk_proactive_snapshot_source_observation_run",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_evaluation_identity_uniqueness(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        run_id = _insert_run(connection)
        connection.execute(_evaluation_statement(), _evaluation_params(run_id))
        _assert_integrity(connection, _evaluation_statement(), _evaluation_params(run_id), "uq_proactive_evaluation_identity")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_evaluation_kpi_check(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        _assert_integrity(connection, _evaluation_statement(), _evaluation_params(_insert_run(connection), kpi_code="KPI-RC-01"), "ck_proactive_evaluation_kpi_code")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_evaluation_run_binding_candidate_key(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        run_id = _insert_run(connection)
        evaluation = _evaluation_params(run_id)
        connection.execute(_evaluation_statement(), evaluation)
    with engine.connect() as connection:
        row = connection.execute(sa.text("SELECT id, analytic_run_id FROM app.proactive_evaluation WHERE id = :id"), {"id": evaluation["id"]}).one()
        assert row == (evaluation["id"], run_id)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_finding_evaluation_same_run_fk(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        run_a, run_b = _insert_run(connection), _insert_run(connection)
        evaluation = _evaluation_params(run_a)
        connection.execute(_evaluation_statement(), evaluation)
        _assert_integrity(
            connection,
            sa.text("INSERT INTO app.finding (id, analytic_run_id, proactive_evaluation_id, finding_type, description, triggered_rule, state) VALUES (:id, :run, :evaluation, 'SIGNAL', 'mismatch', 'RULE', 'OPEN')"),
            {"id": uuid.uuid4(), "run": run_b, "evaluation": evaluation["id"]},
            "fk_finding_proactive_evaluation_same_run",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_one_evaluation_cannot_create_two_findings(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        run_id = _insert_run(connection)
        evaluation = _evaluation_params(run_id)
        connection.execute(_evaluation_statement(), evaluation)
        statement = sa.text("INSERT INTO app.finding (id, analytic_run_id, proactive_evaluation_id, finding_type, description, triggered_rule, state) VALUES (:id, :run, :evaluation, 'SIGNAL', :description, :rule, 'OPEN')")
        connection.execute(statement, {"id": uuid.uuid4(), "run": run_id, "evaluation": evaluation["id"], "description": "first", "rule": "RULE_A"})
        _assert_integrity(connection, statement, {"id": uuid.uuid4(), "run": run_id, "evaluation": evaluation["id"], "description": "second", "rule": "RULE_B"}, "uq_finding_proactive_evaluation")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_overlapping_rule_branches_create_one_finding(proactive_database) -> None:
    test_one_evaluation_cannot_create_two_findings(proactive_database)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_insufficient_history_constraints(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        run_id = _insert_run(connection)
        _assert_integrity(connection, _evaluation_statement(), _evaluation_params(run_id, outcome="INSUFFICIENT_HISTORY", signal_detected=True, recurrence_month_count=None, reason="MISSING"), "ck_proactive_evaluation_insufficient_history")
        _assert_integrity(connection, _evaluation_statement(), _evaluation_params(run_id, outcome="INSUFFICIENT_HISTORY", recurrence_month_count=0, reason="MISSING"), "ck_proactive_evaluation_insufficient_history")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_evaluated_reason_must_be_null(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        _assert_integrity(connection, _evaluation_statement(), _evaluation_params(_insert_run(connection), reason="UNEXPECTED"), "ck_proactive_evaluation_evaluated_reason")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_recurrence_range_constraint(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        _assert_integrity(connection, _evaluation_statement(), _evaluation_params(_insert_run(connection), recurrence_month_count=5), "ck_proactive_evaluation_recurrence_range")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_rules_applied_non_empty(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        _assert_integrity(connection, _evaluation_statement(), _evaluation_params(_insert_run(connection), rules_applied=[]), "ck_proactive_evaluation_rules_nonempty")


@pytest.mark.requires_db
@pytest.mark.data_schema
@pytest.mark.parametrize("case_type", ["PROACTIVE_ANALYSIS", "KPI_RECALCULATION"])
def test_job_run_accepts_proactive_analysis(proactive_database, case_type: str) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        _insert_job(connection, case_type=case_type)


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_unknown_job_case_type_rejected(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        _assert_integrity(connection, sa.text("INSERT INTO app.job_run (id, case_type, state, actor_process, operation_id, correlation_id) VALUES (:id, 'UNKNOWN', 'STARTED', 'test', :operation, :correlation)"), {"id": uuid.uuid4(), "operation": uuid.uuid4(), "correlation": uuid.uuid4()}, "ck_job_run_case")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_snapshot_copy_survives_source_observation_update(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        snapshot = _snapshot_params(target, job, observation, source)
        connection.execute(_snapshot_statement(), snapshot)
        connection.execute(sa.text("UPDATE app.kpi_observation SET value = 99 WHERE id = :id"), {"id": observation})
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT value FROM app.proactive_input_snapshot WHERE id = :id"), {"id": snapshot["id"]}).scalar_one() == 10


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_proactive_snapshot_direct_update_is_rejected(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        snapshot = _snapshot_params(target, job, observation, source)
        connection.execute(_snapshot_statement(), snapshot)
        _assert_integrity(
            connection,
            sa.text("UPDATE app.proactive_input_snapshot SET value = 99 WHERE id = :id"),
            {"id": snapshot["id"]},
            "ck_proactive_snapshot_immutable",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_proactive_snapshot_direct_delete_is_rejected(proactive_database) -> None:
    engine, _ = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        snapshot = _snapshot_params(target, job, observation, source)
        connection.execute(_snapshot_statement(), snapshot)
        _assert_integrity(
            connection,
            sa.text("DELETE FROM app.proactive_input_snapshot WHERE id = :id"),
            {"id": snapshot["id"]},
            "ck_proactive_snapshot_immutable",
        )


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_downgrade_empty_0013_to_0012(proactive_database) -> None:
    engine, config = proactive_database
    command.downgrade(config, "0012_kpi_observation_semantics")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == "0012_kpi_observation_semantics"
    command.upgrade(config, "0013_proactive_analysis")


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_downgrade_with_analysis_evidence_aborts_without_data_loss(proactive_database) -> None:
    engine, config = proactive_database
    with engine.begin() as connection:
        target, job = _insert_run(connection), _insert_job(connection)
        observation, source = _insert_source_observation(connection)
        connection.execute(_snapshot_statement(), _snapshot_params(target, job, observation, source))
    with pytest.raises(RuntimeError, match="MIGRATION_0013_DOWNGRADE_ABORT"):
        command.downgrade(config, "0012_kpi_observation_semantics")
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM app.proactive_input_snapshot")).scalar_one() == 1
