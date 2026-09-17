"""Fundación: pgvector, schemas y objetos binarios referenciados.

Revision ID: 0001_foundation
Revises: None
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")

    op.create_table(
        "stored_object",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("storage_kind", sa.Text(), nullable=False, server_default="FILESYSTEM"),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("storage_kind = 'FILESYSTEM'", name="ck_stored_object_kind"),
        sa.CheckConstraint("locator <> ''", name="ck_stored_object_locator_nonempty"),
        sa.CheckConstraint(
            "locator !~ '(^/|^[A-Za-z]:|(^|/)\\.\\.(/|$))'",
            name="ck_stored_object_locator_relative",
        ),
        sa.CheckConstraint("octet_length(sha256) = 32", name="ck_stored_object_sha256"),
        sa.CheckConstraint("byte_size >= 0", name="ck_stored_object_byte_size"),
        sa.UniqueConstraint("locator", name="uq_stored_object_locator"),
        schema="app",
    )
    op.create_index(
        "ix_stored_object_sha256",
        "stored_object",
        ["sha256"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_stored_object_sha256", table_name="stored_object", schema="app")
    op.drop_table("stored_object", schema="app")
    op.execute("DROP SCHEMA audit")
    op.execute("DROP SCHEMA app")
    op.execute("DROP EXTENSION IF EXISTS vector")
