"""Persistencia reproducible de análisis proactivo.

Revision ID: 0013_proactive_analysis
Revises: 0012_kpi_observation_semantics
"""
from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0013_proactive_analysis"
down_revision: str | None = "0012_kpi_observation_semantics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())

_SNAPSHOT_CODES = "'KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05', 'KPI-CN-02', 'KPI-CN-03', 'KPI-RC-01', 'KPI-EO-01'"
_EVALUATION_CODES = "'KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05', 'KPI-CN-02', 'KPI-CN-03'"


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_kpi_observation_run_binding",
        "kpi_observation",
        ["id", "analytic_run_id"],
        schema="app",
    )

    op.create_table(
        "proactive_input_snapshot",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("analytic_run_id", UUID, sa.ForeignKey("app.analytic_run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("job_run_id", UUID, sa.ForeignKey("app.job_run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_observation_id", UUID, nullable=False),
        sa.Column("source_analytic_run_id", UUID, nullable=False),
        sa.Column("kpi_code", sa.Text(), nullable=False),
        sa.Column("canonical_dimensions_key", JSONB, nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("availability", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(f"kpi_code IN ({_SNAPSHOT_CODES})", name="ck_proactive_snapshot_kpi_code"),
        sa.CheckConstraint("EXTRACT(DAY FROM period_start) = 1", name="ck_proactive_snapshot_period_start_monthly"),
        sa.CheckConstraint("period_end = (period_start + INTERVAL '1 month' - INTERVAL '1 day')::date", name="ck_proactive_snapshot_period_end_monthly"),
        sa.CheckConstraint("jsonb_typeof(canonical_dimensions_key) = 'object'", name="ck_proactive_snapshot_dimensions_object"),
        sa.CheckConstraint("(availability = 'DISPONIBLE' AND value IS NOT NULL) OR (availability = 'NO_DISPONIBLE' AND value IS NULL)", name="ck_proactive_snapshot_availability"),
        sa.UniqueConstraint("job_run_id", "source_observation_id", name="uq_proactive_snapshot_job_source"),
        sa.UniqueConstraint("job_run_id", "kpi_code", "period_start", "period_end", "canonical_dimensions_key", name="uq_proactive_snapshot_job_logical"),
        sa.ForeignKeyConstraint(
            ["source_observation_id", "source_analytic_run_id"],
            ["app.kpi_observation.id", "app.kpi_observation.analytic_run_id"],
            name="fk_proactive_snapshot_source_observation_run",
            ondelete="RESTRICT",
        ),
        schema="app",
    )
    op.create_index("ix_proactive_snapshot_run_series", "proactive_input_snapshot", ["analytic_run_id", "kpi_code", "period_start"], schema="app")

    op.execute(
        """
        CREATE FUNCTION app.enforce_proactive_snapshot_job()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_case_type text;
            parent_state text;
        BEGIN
            SELECT case_type, state
              INTO parent_case_type, parent_state
              FROM app.job_run
             WHERE id = NEW.job_run_id
             FOR UPDATE;

            IF FOUND AND parent_case_type <> 'PROACTIVE_ANALYSIS' THEN
                RAISE EXCEPTION 'proactive input snapshots require a PROACTIVE_ANALYSIS job'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_proactive_snapshot_job_case';
            END IF;

            IF FOUND AND parent_state = 'NO_RELEVANT_WORK' THEN
                RAISE EXCEPTION 'NO_RELEVANT_WORK jobs cannot own proactive input snapshots'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_proactive_snapshot_job_state';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_proactive_snapshot_job
        BEFORE INSERT OR UPDATE OF job_run_id
        ON app.proactive_input_snapshot
        FOR EACH ROW
        EXECUTE FUNCTION app.enforce_proactive_snapshot_job()
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.prevent_job_no_relevant_work_with_snapshots()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.state = 'NO_RELEVANT_WORK'
               AND OLD.state IS DISTINCT FROM NEW.state
               AND EXISTS (
                   SELECT 1
                     FROM app.proactive_input_snapshot
                    WHERE job_run_id = NEW.id
               ) THEN
                RAISE EXCEPTION 'jobs with proactive input snapshots cannot transition to NO_RELEVANT_WORK'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_job_run_no_relevant_work_without_snapshots';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_job_no_relevant_work_without_snapshots
        BEFORE UPDATE OF state
        ON app.job_run
        FOR EACH ROW
        EXECUTE FUNCTION app.prevent_job_no_relevant_work_with_snapshots()
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.prevent_proactive_snapshot_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'proactive input snapshots are immutable'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_proactive_snapshot_immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_proactive_snapshot_immutable
        BEFORE UPDATE OR DELETE
        ON app.proactive_input_snapshot
        FOR EACH ROW
        EXECUTE FUNCTION app.prevent_proactive_snapshot_mutation()
        """
    )

    op.create_table(
        "proactive_evaluation",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("analytic_run_id", UUID, sa.ForeignKey("app.analytic_run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kpi_code", sa.Text(), nullable=False),
        sa.Column("canonical_dimensions_key", JSONB, nullable=False),
        sa.Column("evaluation_period", sa.Date(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("signal_detected", sa.Boolean(), nullable=False),
        sa.Column("trend_direction", sa.Text(), nullable=True),
        sa.Column("recurrence_month_count", sa.SmallInteger(), nullable=True),
        sa.Column("current_value", sa.Numeric(), nullable=True),
        sa.Column("reference_value", sa.Numeric(), nullable=True),
        sa.Column("absolute_variation", sa.Numeric(), nullable=True),
        sa.Column("percentage_variation", sa.Numeric(), nullable=True),
        sa.Column("rules_applied", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("not_evaluated_reason", sa.Text(), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(f"kpi_code IN ({_EVALUATION_CODES})", name="ck_proactive_evaluation_kpi_code"),
        sa.CheckConstraint("jsonb_typeof(canonical_dimensions_key) = 'object'", name="ck_proactive_evaluation_dimensions_object"),
        sa.CheckConstraint("EXTRACT(DAY FROM evaluation_period) = 1", name="ck_proactive_evaluation_period_monthly"),
        sa.CheckConstraint("outcome IN ('EVALUATED', 'INSUFFICIENT_HISTORY')", name="ck_proactive_evaluation_outcome"),
        sa.CheckConstraint("outcome <> 'INSUFFICIENT_HISTORY' OR (signal_detected = false AND trend_direction IS NULL AND recurrence_month_count IS NULL AND not_evaluated_reason IS NOT NULL)", name="ck_proactive_evaluation_insufficient_history"),
        sa.CheckConstraint("outcome <> 'EVALUATED' OR not_evaluated_reason IS NULL", name="ck_proactive_evaluation_evaluated_reason"),
        sa.CheckConstraint("trend_direction IS NULL OR trend_direction IN ('INCREASING', 'DECREASING')", name="ck_proactive_evaluation_trend_direction"),
        sa.CheckConstraint("recurrence_month_count IS NULL OR recurrence_month_count BETWEEN 0 AND 4", name="ck_proactive_evaluation_recurrence_range"),
        sa.CheckConstraint("cardinality(rules_applied) > 0", name="ck_proactive_evaluation_rules_nonempty"),
        sa.UniqueConstraint("analytic_run_id", "kpi_code", "canonical_dimensions_key", "evaluation_period", name="uq_proactive_evaluation_identity"),
        sa.UniqueConstraint("id", "analytic_run_id", name="uq_proactive_evaluation_run_binding"),
        schema="app",
    )
    op.create_index("ix_proactive_evaluation_series", "proactive_evaluation", ["kpi_code", "evaluation_period"], schema="app")

    op.add_column("finding", sa.Column("proactive_evaluation_id", UUID, nullable=True), schema="app")
    op.create_foreign_key(
        "fk_finding_proactive_evaluation_same_run",
        "finding",
        "proactive_evaluation",
        ["proactive_evaluation_id", "analytic_run_id"],
        ["id", "analytic_run_id"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_finding_proactive_evaluation",
        "finding",
        ["proactive_evaluation_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("proactive_evaluation_id IS NOT NULL"),
    )


def downgrade() -> None:
    if not context.is_offline_mode():
        bind = op.get_bind()
        has_evidence = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM app.proactive_input_snapshot) "
                "OR EXISTS (SELECT 1 FROM app.proactive_evaluation) "
                "OR EXISTS (SELECT 1 FROM app.finding WHERE proactive_evaluation_id IS NOT NULL)"
            )
        ).scalar()
        if has_evidence:
            raise RuntimeError(
                "MIGRATION_0013_DOWNGRADE_ABORT: proactive analysis evidence exists; "
                "manual reconciliation required before downgrade."
            )

    op.drop_index("uq_finding_proactive_evaluation", table_name="finding", schema="app")
    op.drop_constraint("fk_finding_proactive_evaluation_same_run", "finding", schema="app", type_="foreignkey")
    op.drop_column("finding", "proactive_evaluation_id", schema="app")
    op.drop_index("ix_proactive_evaluation_series", table_name="proactive_evaluation", schema="app")
    op.drop_table("proactive_evaluation", schema="app")
    op.execute("DROP TRIGGER IF EXISTS trg_job_no_relevant_work_without_snapshots ON app.job_run")
    op.execute("DROP FUNCTION IF EXISTS app.prevent_job_no_relevant_work_with_snapshots()")
    op.execute("DROP TRIGGER IF EXISTS trg_proactive_snapshot_immutable ON app.proactive_input_snapshot")
    op.execute("DROP FUNCTION IF EXISTS app.prevent_proactive_snapshot_mutation()")
    op.execute("DROP TRIGGER IF EXISTS trg_proactive_snapshot_job ON app.proactive_input_snapshot")
    op.execute("DROP FUNCTION IF EXISTS app.enforce_proactive_snapshot_job()")
    op.drop_index("ix_proactive_snapshot_run_series", table_name="proactive_input_snapshot", schema="app")
    op.drop_table("proactive_input_snapshot", schema="app")
    op.execute(
        "ALTER TABLE app.kpi_observation "
        "DROP CONSTRAINT IF EXISTS uq_kpi_observation_run_binding"
    )
