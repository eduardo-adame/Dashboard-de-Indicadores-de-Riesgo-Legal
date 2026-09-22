"""Semántica temporal determinista de las observaciones KPI.

Alinea ``app.kpi_observation`` para soportar la semántica mensual canónica del
módulo de cálculo de indicadores: exige la fecha efectiva de cálculo
(``as_of_date``) y garantiza que cada observación represente un mes natural
completo. No crea ni recrea entidades analíticas: la foundation física ya existe
en las revisiones 0002 y 0004.

La revisión aborta de forma explícita si ``app.kpi_observation`` contiene filas
inesperadas, para no fabricar valores históricos ni alterar datos existentes sin
reconciliación.

Revision ID: 0012_kpi_observation_semantics
Revises: 0011_document_manage_capability
"""
from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa


revision: str = "0012_kpi_observation_semantics"
down_revision: str | None = "0011_document_manage_capability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # La conexión la administra Alembic; no se abre una conexión adicional ni se
    # depende de la configuración runtime de la aplicación. En modo offline no
    # existe conexión y no hay datos que verificar.
    if not context.is_offline_mode():
        bind = op.get_bind()
        has_rows = bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM app.kpi_observation LIMIT 1)")
        ).scalar()
        if has_rows:
            raise RuntimeError(
                "MIGRATION_0012_ABORT: app.kpi_observation contains unexpected rows. "
                "Expected empty table for safe schema alignment. "
                "Manual reconciliation required before proceeding."
            )

    op.add_column(
        "kpi_observation",
        sa.Column("as_of_date", sa.Date(), nullable=False),
        schema="app",
    )
    op.create_check_constraint(
        "ck_kpi_observation_period_start_monthly",
        "kpi_observation",
        "EXTRACT(DAY FROM period_start) = 1",
        schema="app",
    )
    op.create_check_constraint(
        "ck_kpi_observation_period_end_monthly",
        "kpi_observation",
        "period_end = (period_start + INTERVAL '1 month' - INTERVAL '1 day')::date",
        schema="app",
    )
    op.create_check_constraint(
        "ck_kpi_observation_dimensions_object",
        "kpi_observation",
        "jsonb_typeof(dimensions) = 'object'",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_kpi_observation_dimensions_object", "kpi_observation", schema="app"
    )
    op.drop_constraint(
        "ck_kpi_observation_period_end_monthly", "kpi_observation", schema="app"
    )
    op.drop_constraint(
        "ck_kpi_observation_period_start_monthly", "kpi_observation", schema="app"
    )
    op.drop_column("kpi_observation", "as_of_date", schema="app")
