"""Resultados analíticos, RAG persistible y ejecuciones coordinadas.

Revision ID: 0004_analytics_rag_jobs
Revises: 0003_documents_corpus
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0004_analytics_rag_jobs"
down_revision: str | None = "0003_documents_corpus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "analytic_run",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("run_type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kpi_codes", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("dimensions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("rules_reference", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("not_evaluated_reason", sa.Text(), nullable=True),
        sa.Column("executive_summary", sa.Text(), nullable=True),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("run_type IN ('KPI_RECALCULATION', 'PROACTIVE_ANALYSIS')", name="ck_analytic_run_type"),
        sa.CheckConstraint("state IN ('STARTED', 'COMPLETED', 'FAILED')", name="ck_analytic_run_state"),
        sa.CheckConstraint("window_end IS NULL OR window_start IS NULL OR window_end >= window_start", name="ck_analytic_run_window"),
        sa.CheckConstraint(
            "(state = 'STARTED' AND completed_at IS NULL) OR (state IN ('COMPLETED', 'FAILED') AND completed_at IS NOT NULL)",
            name="ck_analytic_run_completion",
        ),
        sa.UniqueConstraint("operation_id", name="uq_analytic_run_operation"),
        schema="app",
    )

    op.create_table(
        "kpi_observation",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("analytic_run_id", UUID, sa.ForeignKey("app.analytic_run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kpi_code", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("dimensions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("availability", sa.Text(), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "kpi_code IN ('KPI-RC-01', 'KPI-RC-03', 'KPI-LI-01', 'KPI-LI-05', 'KPI-CN-02', 'KPI-CN-03', 'KPI-EO-01', 'KPI-CD-03')",
            name="ck_kpi_observation_code",
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_kpi_observation_period"),
        sa.CheckConstraint(
            "(availability = 'DISPONIBLE' AND value IS NOT NULL) OR "
            "(availability = 'NO_DISPONIBLE' AND value IS NULL)",
            name="ck_kpi_observation_availability",
        ),
        sa.UniqueConstraint("kpi_code", "period_start", "period_end", "dimensions", name="uq_kpi_observation_logical"),
        schema="app",
    )
    op.create_index("ix_kpi_observation_period", "kpi_observation", ["kpi_code", "period_start", "period_end"], schema="app")

    op.create_table(
        "finding",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("analytic_run_id", UUID, sa.ForeignKey("app.analytic_run.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("finding_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kpi_code", sa.Text(), nullable=True),
        sa.Column("dimensions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("current_value", sa.Numeric(), nullable=True),
        sa.Column("reference_value", sa.Numeric(), nullable=True),
        sa.Column("variation", sa.Numeric(), nullable=True),
        sa.Column("triggered_rule", sa.Text(), nullable=False),
        sa.Column("recurrent_pattern", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("description <> ''", name="ck_finding_description_nonempty"),
        sa.CheckConstraint("triggered_rule <> ''", name="ck_finding_rule_nonempty"),
        sa.CheckConstraint("period_end IS NULL OR period_start IS NULL OR period_end >= period_start", name="ck_finding_period"),
        schema="app",
    )
    op.create_index("ix_finding_run", "finding", ["analytic_run_id"], schema="app")

    op.create_table(
        "context_reference",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("finding_id", UUID, sa.ForeignKey("app.finding.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kpi_code", sa.Text(), nullable=True),
        sa.Column("dimensions", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("context_identifiers", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("period_end IS NULL OR period_start IS NULL OR period_end >= period_start", name="ck_context_reference_period"),
        sa.UniqueConstraint("finding_id", name="uq_context_reference_finding"),
        sa.UniqueConstraint("operation_id", name="uq_context_reference_operation"),
        schema="app",
    )

    op.create_table(
        "rag_operation",
        sa.Column("id", UUID, primary_key=True),
        # La FK a user_account se agrega en 0005, cuando ese objeto ya existe.
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("session_id", UUID, nullable=True),
        sa.Column("query_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("generated_response", sa.Text(), nullable=True),
        sa.Column("safe_result_message", sa.Text(), nullable=True),
        sa.Column("context_reference_id", UUID, sa.ForeignKey("app.context_reference.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("octet_length(query_sha256) = 32", name="ck_rag_operation_query_hash"),
        sa.CheckConstraint(
            "state IN ('EVIDENCIA_SUFICIENTE', 'EVIDENCIA_INSUFICIENTE', 'SIN_EVIDENCIA', 'SIN_AUTORIZACION')",
            name="ck_rag_operation_state",
        ),
        sa.CheckConstraint(
            "generated_response IS NULL OR state = 'EVIDENCIA_SUFICIENTE'",
            name="ck_rag_operation_response_state",
        ),
        sa.UniqueConstraint("operation_id", name="uq_rag_operation_operation"),
        schema="app",
    )
    op.create_index("ix_rag_operation_user_time", "rag_operation", ["user_id", "occurred_at"], schema="app")

    op.create_table(
        "rag_final_fragment",
        sa.Column("rag_operation_id", UUID, sa.ForeignKey("app.rag_operation.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("fragment_id", UUID, sa.ForeignKey("app.document_chunk.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("usage", sa.Text(), nullable=False),
        sa.Column("citation_document_name", sa.Text(), nullable=False),
        sa.Column("citation_document_type", sa.Text(), nullable=False),
        sa.Column("citation_document_date", sa.Date(), nullable=True),
        sa.Column("citation_page_start", sa.Integer(), nullable=True),
        sa.Column("citation_page_end", sa.Integer(), nullable=True),
        sa.Column("citation_section", sa.Text(), nullable=True),
        sa.Column("citation_clause", sa.Text(), nullable=True),
        sa.CheckConstraint("usage IN ('CONTEXT', 'EVIDENCE', 'REFERENCE')", name="ck_rag_final_fragment_usage"),
        sa.CheckConstraint("citation_page_start IS NULL OR citation_page_start > 0", name="ck_rag_final_fragment_page_start"),
        sa.CheckConstraint("citation_page_end IS NULL OR citation_page_end > 0", name="ck_rag_final_fragment_page_end"),
        sa.CheckConstraint(
            "citation_page_start IS NULL OR citation_page_end IS NULL OR citation_page_end >= citation_page_start",
            name="ck_rag_final_fragment_page_range",
        ),
        schema="app",
    )
    # La selección limita el contexto a cinco fragmentos; no se persisten
    # posiciones, puntuaciones ni candidatos descartados.

    op.create_table(
        "job_run",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_type", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("actor_process", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_references", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("safe_error_code", sa.Text(), nullable=True),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("case_type IN ('INGESTION', 'OCR', 'KPI_RECALCULATION', 'PROACTIVE_ANALYSIS')", name="ck_job_run_case"),
        sa.CheckConstraint("state IN ('STARTED', 'COMPLETED', 'FAILED', 'NO_RELEVANT_WORK')", name="ck_job_run_state"),
        sa.CheckConstraint("window_end IS NULL OR window_start IS NULL OR window_end >= window_start", name="ck_job_run_window"),
        sa.UniqueConstraint("operation_id", name="uq_job_run_operation"),
        schema="app",
    )
    op.create_index("ix_job_run_correlation", "job_run", ["correlation_id"], schema="app")


def downgrade() -> None:
    op.drop_index("ix_job_run_correlation", table_name="job_run", schema="app")
    op.drop_table("job_run", schema="app")
    op.drop_table("rag_final_fragment", schema="app")
    op.drop_index("ix_rag_operation_user_time", table_name="rag_operation", schema="app")
    op.drop_table("rag_operation", schema="app")
    op.drop_table("context_reference", schema="app")
    op.drop_index("ix_finding_run", table_name="finding", schema="app")
    op.drop_table("finding", schema="app")
    op.drop_index("ix_kpi_observation_period", table_name="kpi_observation", schema="app")
    op.drop_table("kpi_observation", schema="app")
    op.drop_table("analytic_run", schema="app")
