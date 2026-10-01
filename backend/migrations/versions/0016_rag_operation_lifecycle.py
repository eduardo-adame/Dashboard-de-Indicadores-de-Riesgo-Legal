"""Formaliza el ciclo de vida terminal de las operaciones RAG.

ID de revisión: 0016_rag_operation_lifecycle
Revisa: 0015_runtime_privs
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0016_rag_operation_lifecycle"
down_revision: str | None = "0015_runtime_privs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EVIDENCE_STATES = (
    "EVIDENCIA_SUFICIENTE",
    "EVIDENCIA_INSUFICIENTE",
    "SIN_EVIDENCIA",
    "SIN_AUTORIZACION",
)

_SAFE_CAUSES = (
    "EMBEDDING_UNAVAILABLE",
    "RETRIEVAL_FAILED",
    "PROVIDER_RATE_LIMITED",
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "INVALID_PROVIDER_OUTPUT",
    "CONTEXT_LIMIT_EXCEEDED",
    "INTERNAL_FAILURE",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    # El esquema anterior permite filas ambiguas. El upgrade aborta antes de
    # modificarlas para no inventar resultados terminales durante el backfill.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM app.rag_operation
            WHERE
              (state = 'EVIDENCIA_SUFICIENTE' AND (
                generated_response IS NULL
                OR btrim(generated_response) = ''
                OR safe_result_message IS NOT NULL
              ))
              OR
              (state IN ('EVIDENCIA_INSUFICIENTE', 'SIN_EVIDENCIA', 'SIN_AUTORIZACION') AND (
                generated_response IS NOT NULL
                OR safe_result_message IS NULL
                OR btrim(safe_result_message) = ''
              ))
          ) THEN
            RAISE EXCEPTION
              'MIGRATION_0016_UPGRADE_ABORT: ambiguous legacy rag_operation row exists; manual reconciliation required'
              USING ERRCODE = '23514';
          END IF;
        END
        $$
        """
    )

    op.add_column("rag_operation", sa.Column("operation_status", sa.Text(), nullable=True), schema="app")
    op.add_column("rag_operation", sa.Column("generation_status", sa.Text(), nullable=True), schema="app")
    op.add_column("rag_operation", sa.Column("safe_cause_code", sa.Text(), nullable=True), schema="app")

    op.execute(
        """
        UPDATE app.rag_operation
        SET operation_status = 'COMPLETED',
            generation_status = CASE
              WHEN state = 'EVIDENCIA_SUFICIENTE' THEN 'SUCCEEDED'
              ELSE 'NOT_REQUESTED'
            END
        """
    )

    op.drop_constraint("ck_rag_operation_response_state", "rag_operation", schema="app", type_="check")
    op.drop_constraint("ck_rag_operation_state", "rag_operation", schema="app", type_="check")
    op.alter_column("rag_operation", "state", schema="app", existing_type=sa.Text(), nullable=True)
    op.alter_column(
        "rag_operation",
        "operation_status",
        schema="app",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.alter_column(
        "rag_operation",
        "generation_status",
        schema="app",
        existing_type=sa.Text(),
        nullable=False,
    )

    op.create_check_constraint(
        "ck_rag_operation_state",
        "rag_operation",
        f"state IS NULL OR state IN ({_quoted(_EVIDENCE_STATES)})",
        schema="app",
    )
    op.create_check_constraint(
        "ck_rag_operation_operation_status",
        "rag_operation",
        "operation_status IN ('COMPLETED', 'FAILED')",
        schema="app",
    )
    op.create_check_constraint(
        "ck_rag_operation_generation_status",
        "rag_operation",
        "generation_status IN ('NOT_REQUESTED', 'SUCCEEDED', 'FAILED')",
        schema="app",
    )
    op.create_check_constraint(
        "ck_rag_operation_safe_cause",
        "rag_operation",
        f"safe_cause_code IS NULL OR safe_cause_code IN ({_quoted(_SAFE_CAUSES)})",
        schema="app",
    )
    op.create_check_constraint(
        "ck_rag_operation_lifecycle_consistency",
        "rag_operation",
        """
        (
          (
            state IS NULL
            AND operation_status = 'FAILED'
            AND generation_status = 'NOT_REQUESTED'
            AND safe_cause_code IS NOT NULL
            AND generated_response IS NULL
            AND safe_result_message IS NOT NULL
            AND btrim(safe_result_message) <> ''
          )
          OR
          (
            state IS NOT NULL
            AND (
              (
                state = 'EVIDENCIA_SUFICIENTE'
                AND operation_status = 'COMPLETED'
                AND generation_status = 'SUCCEEDED'
                AND safe_cause_code IS NULL
                AND generated_response IS NOT NULL
                AND btrim(generated_response) <> ''
                AND safe_result_message IS NULL
              )
              OR
              (
                state = 'EVIDENCIA_SUFICIENTE'
                AND operation_status = 'FAILED'
                AND generation_status = 'FAILED'
                AND safe_cause_code IS NOT NULL
                AND generated_response IS NULL
                AND safe_result_message IS NOT NULL
                AND btrim(safe_result_message) <> ''
              )
              OR
              (
                state IN ('EVIDENCIA_INSUFICIENTE', 'SIN_EVIDENCIA', 'SIN_AUTORIZACION')
                AND operation_status = 'COMPLETED'
                AND generation_status = 'NOT_REQUESTED'
                AND safe_cause_code IS NULL
                AND generated_response IS NULL
                AND safe_result_message IS NOT NULL
                AND btrim(safe_result_message) <> ''
              )
            )
          )
        ) IS TRUE
        """,
        schema="app",
    )


def downgrade() -> None:
    # 0015 no puede representar fallos terminales ni operaciones sin estado de
    # evidencia. El downgrade se niega antes de descartar esa información.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM app.rag_operation
            WHERE operation_status <> 'COMPLETED'
               OR state IS NULL
               OR safe_cause_code IS NOT NULL
               OR (state = 'EVIDENCIA_SUFICIENTE' AND generation_status <> 'SUCCEEDED')
               OR (
                 state IN ('EVIDENCIA_INSUFICIENTE', 'SIN_EVIDENCIA', 'SIN_AUTORIZACION')
                 AND generation_status <> 'NOT_REQUESTED'
               )
          ) THEN
            RAISE EXCEPTION
              'MIGRATION_0016_DOWNGRADE_ABORT: rag_operation contains lifecycle evidence not representable by 0015'
              USING ERRCODE = '23514';
          END IF;
        END
        $$
        """
    )

    op.drop_constraint("ck_rag_operation_lifecycle_consistency", "rag_operation", schema="app", type_="check")
    op.drop_constraint("ck_rag_operation_safe_cause", "rag_operation", schema="app", type_="check")
    op.drop_constraint("ck_rag_operation_generation_status", "rag_operation", schema="app", type_="check")
    op.drop_constraint("ck_rag_operation_operation_status", "rag_operation", schema="app", type_="check")
    op.drop_constraint("ck_rag_operation_state", "rag_operation", schema="app", type_="check")

    op.alter_column("rag_operation", "state", schema="app", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_rag_operation_state",
        "rag_operation",
        f"state IN ({_quoted(_EVIDENCE_STATES)})",
        schema="app",
    )
    op.create_check_constraint(
        "ck_rag_operation_response_state",
        "rag_operation",
        "generated_response IS NULL OR state = 'EVIDENCIA_SUFICIENTE'",
        schema="app",
    )

    op.drop_column("rag_operation", "safe_cause_code", schema="app")
    op.drop_column("rag_operation", "generation_status", schema="app")
    op.drop_column("rag_operation", "operation_status", schema="app")
