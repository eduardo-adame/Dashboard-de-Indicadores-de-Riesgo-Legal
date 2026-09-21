"""Persistencia de la coordinación de despacho downstream.

Registra, por cada recepción de archivo y destino downstream, una única
operación lógica con identidad estable. La identidad idempotente es
``(file_id, downstream_target)``; ``operation_id`` se genera una sola vez y se
propaga como identidad de la operación funcional downstream. ``correlation_id``
se conserva solo para trazabilidad.

Revision ID: 0009_coordination_dispatch
Revises: 0008_ingestion_resource_receipts
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009_coordination_dispatch"
down_revision: str | None = "0008_ingestion_resource_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "coordination_dispatch",
        sa.Column("id", UUID, primary_key=True),
        # Identidad funcional estable: se genera una vez y se propaga downstream.
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column(
            "file_id",
            UUID,
            sa.ForeignKey("app.ingest_file.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("downstream_target", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="NEW"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        # Trazabilidad únicamente; no participa en la identidad idempotente.
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        # Referencia segura al resultado downstream; nunca el payload funcional.
        sa.Column("downstream_result_id", UUID, nullable=True),
        sa.Column("safe_cause_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "downstream_target IN ('VALIDATION', 'DOCUMENT')",
            name="ck_coordination_dispatch_target",
        ),
        sa.CheckConstraint(
            "state IN ('NEW', 'IN_PROGRESS', 'COMPLETED', 'FAILED')",
            name="ck_coordination_dispatch_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_coordination_dispatch_attempts"),
        sa.CheckConstraint(
            "completed_at IS NULL OR started_at IS NOT NULL",
            name="ck_coordination_dispatch_completion_order",
        ),
        sa.UniqueConstraint("file_id", "downstream_target", name="uq_coordination_dispatch_identity"),
        sa.UniqueConstraint("operation_id", name="uq_coordination_dispatch_operation"),
        schema="app",
    )
    op.create_index(
        "ix_coordination_dispatch_state",
        "coordination_dispatch",
        ["state"],
        schema="app",
    )
    op.create_index(
        "ix_coordination_dispatch_correlation",
        "coordination_dispatch",
        ["correlation_id"],
        schema="app",
    )
    op.create_index(
        "ix_coordination_dispatch_heartbeat",
        "coordination_dispatch",
        ["heartbeat_at"],
        schema="app",
        postgresql_where=sa.text("state = 'IN_PROGRESS'"),
    )


def downgrade() -> None:
    op.drop_index("ix_coordination_dispatch_heartbeat", table_name="coordination_dispatch", schema="app")
    op.drop_index("ix_coordination_dispatch_correlation", table_name="coordination_dispatch", schema="app")
    op.drop_index("ix_coordination_dispatch_state", table_name="coordination_dispatch", schema="app")
    op.drop_table("coordination_dispatch", schema="app")
