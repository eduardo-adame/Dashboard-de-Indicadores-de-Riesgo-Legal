"""Persistencia durable del candidato documental con fidelidad por página.

Representa el resultado de la extracción documental inicial de forma que
``persist → reload`` conserve el texto nativo total, el conjunto y orden de
páginas, el texto nativo por página, la necesidad de OCR por página y el estado
de procesamiento.

Revision ID: 0010_document_candidate
Revises: 0009_coordination_dispatch
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0010_document_candidate"
down_revision: str | None = "0009_coordination_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "document_candidate",
        sa.Column(
            "ingest_file_id",
            UUID,
            sa.ForeignKey("app.ingest_file.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("processing_state", sa.Text(), nullable=False),
        sa.Column("native_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "processing_state IN ('PENDING_OCR', 'NATIVE_TEXT')",
            name="ck_document_candidate_state",
        ),
        schema="app",
    )

    op.create_table(
        "document_candidate_page",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "ingest_file_id",
            UUID,
            sa.ForeignKey("app.document_candidate.ingest_file_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("native_text", sa.Text(), nullable=False),
        sa.Column("requires_ocr", sa.Boolean(), nullable=False),
        sa.CheckConstraint("page_number > 0", name="ck_document_candidate_page_number"),
        sa.UniqueConstraint("ingest_file_id", "page_number", name="uq_document_candidate_page"),
        schema="app",
    )
    op.create_index(
        "ix_document_candidate_page_file",
        "document_candidate_page",
        ["ingest_file_id"],
        schema="app",
    )


def downgrade() -> None:
    op.drop_index("ix_document_candidate_page_file", table_name="document_candidate_page", schema="app")
    op.drop_table("document_candidate_page", schema="app")
    op.drop_table("document_candidate", schema="app")
