"""Documentos versionados, OCR, chunks y soporte vectorial/léxico.

Revision ID: 0003_documents_corpus
Revises: 0002_ingestion_domain
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql


revision: str = "0003_documents_corpus"
down_revision: str | None = "0002_ingestion_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "document",
        sa.Column("id_documento", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("source_family", sa.Text(), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column("active_version_id", UUID, nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("id_documento <> ''", name="ck_document_id_nonempty"),
        sa.CheckConstraint("name <> ''", name="ck_document_name_nonempty"),
        sa.CheckConstraint("document_type <> ''", name="ck_document_type_nonempty"),
        sa.CheckConstraint(
            "source_family IN ('CONTRATOS_DOCUMENTOS', 'LITIGIOS', 'CUMPLIMIENTO', 'AUDITORIA_INTERNA')",
            name="ck_document_family",
        ),
        sa.CheckConstraint(
            "invalidated_at IS NULL OR active_version_id IS NULL",
            name="ck_document_invalidation_removes_active",
        ),
        schema="app",
    )

    op.create_table(
        "document_version",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("id_documento", sa.Text(), sa.ForeignKey("app.document.id_documento", ondelete="RESTRICT"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("stored_object_id", UUID, sa.ForeignKey("app.stored_object.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("processing_state", sa.Text(), nullable=False),
        sa.Column("native_text", sa.Text(), nullable=True),
        sa.Column("consolidated_text", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version_number > 0", name="ck_document_version_number"),
        sa.CheckConstraint(
            "processing_state IN ('PENDIENTE', 'PROCESANDO', 'LISTA', 'RECHAZADA', 'FALLIDA')",
            name="ck_document_version_state",
        ),
        sa.CheckConstraint("octet_length(content_sha256) = 32", name="ck_document_version_sha256"),
        sa.CheckConstraint(
            "(processing_state IN ('PENDIENTE', 'PROCESANDO') AND completed_at IS NULL) "
            "OR (processing_state IN ('LISTA', 'RECHAZADA', 'FALLIDA') AND completed_at IS NOT NULL)",
            name="ck_document_version_completion",
        ),
        sa.UniqueConstraint("id_documento", "version_number", name="uq_document_version_number"),
        sa.UniqueConstraint("id_documento", "id", name="uq_document_version_document_id"),
        sa.UniqueConstraint("operation_id", name="uq_document_version_operation"),
        schema="app",
    )
    op.create_foreign_key(
        "fk_document_active_version",
        "document",
        "document_version",
        ["id_documento", "active_version_id"],
        ["id_documento", "id"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index("ix_document_version_state", "document_version", ["processing_state"], schema="app")

    op.create_table(
        "ocr_run",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("document_version_id", UUID, sa.ForeignKey("app.document_version.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("estado_ocr", sa.Text(), nullable=False),
        sa.Column("resultado_ocr", sa.Text(), nullable=True),
        sa.Column("confianza_agregada", sa.Numeric(), nullable=True),
        sa.Column("total_page_count", sa.Integer(), nullable=True),
        sa.Column("ocr_processed_page_count", sa.Integer(), nullable=True),
        sa.Column("granularity", sa.Text(), nullable=True),
        sa.Column("is_final", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("attempt_number > 0", name="ck_ocr_run_attempt"),
        sa.CheckConstraint("total_page_count IS NULL OR total_page_count >= 0", name="ck_ocr_run_total_page_count"),
        sa.CheckConstraint(
            "ocr_processed_page_count IS NULL OR ocr_processed_page_count >= 0",
            name="ck_ocr_run_processed_page_count",
        ),
        sa.CheckConstraint(
            "ocr_processed_page_count IS NULL OR "
            "(total_page_count IS NOT NULL AND ocr_processed_page_count <= total_page_count)",
            name="ck_ocr_run_processed_pages_within_total",
        ),
        sa.CheckConstraint(
            "confianza_agregada IS NULL OR (confianza_agregada >= 0 AND confianza_agregada <= 1)",
            name="ck_ocr_run_confidence_range",
        ),
        sa.CheckConstraint(
            "(estado_ocr = 'Pendiente' AND confianza_agregada IS NULL) "
            "OR (estado_ocr = 'Exitoso' AND confianza_agregada >= 0.80) "
            "OR (estado_ocr = 'Rechazado por baja confianza' AND confianza_agregada < 0.80)",
            name="ck_ocr_run_state_confidence",
        ),
        sa.UniqueConstraint("document_version_id", "attempt_number", name="uq_ocr_run_attempt"),
        sa.UniqueConstraint("operation_id", name="uq_ocr_run_operation"),
        schema="app",
    )
    op.create_index(
        "uq_ocr_run_final_version",
        "ocr_run",
        ["document_version_id"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("is_final"),
    )

    op.create_table(
        "ocr_confidence_sample",
        sa.Column("ocr_run_id", UUID, sa.ForeignKey("app.ocr_run.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("unit_number", sa.Integer(), primary_key=True),
        sa.Column("unit_type", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=False),
        sa.CheckConstraint("unit_number > 0", name="ck_ocr_sample_unit"),
        sa.CheckConstraint("unit_type <> ''", name="ck_ocr_sample_type_nonempty"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_ocr_sample_confidence"),
        schema="app",
    )

    op.create_table(
        "document_chunk",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("document_version_id", UUID, sa.ForeignKey("app.document_version.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("clause", sa.Text(), nullable=True),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("ordinal >= 0", name="ck_document_chunk_ordinal"),
        sa.CheckConstraint("content <> ''", name="ck_document_chunk_content_nonempty"),
        sa.CheckConstraint("token_count > 0", name="ck_document_chunk_token_count"),
        sa.CheckConstraint("page_start IS NULL OR page_start > 0", name="ck_document_chunk_page_start"),
        sa.CheckConstraint("page_end IS NULL OR page_end > 0", name="ck_document_chunk_page_end"),
        sa.CheckConstraint(
            "page_start IS NULL OR page_end IS NULL OR page_end >= page_start",
            name="ck_document_chunk_page_range",
        ),
        sa.UniqueConstraint("document_version_id", "ordinal", name="uq_document_chunk_ordinal"),
        schema="app",
    )
    op.create_index("ix_document_chunk_version", "document_chunk", ["document_version_id"], schema="app")
    op.execute(
        "CREATE INDEX ix_document_chunk_embedding_hnsw ON app.document_chunk "
        "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
    )

    op.create_table(
        "chunk_term",
        sa.Column("fragment_id", UUID, sa.ForeignKey("app.document_chunk.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("normalized_lexeme", sa.Text(), primary_key=True),
        sa.Column("term_frequency", sa.Integer(), nullable=False),
        sa.CheckConstraint("normalized_lexeme <> ''", name="ck_chunk_term_lexeme_nonempty"),
        sa.CheckConstraint("term_frequency > 0", name="ck_chunk_term_frequency"),
        schema="app",
    )
    op.create_index("ix_chunk_term_lexeme_fragment", "chunk_term", ["normalized_lexeme", "fragment_id"], schema="app")

    op.execute(
        """
        CREATE VIEW app.active_document_chunk AS
        SELECT c.*
          FROM app.document_chunk AS c
          JOIN app.document_version AS v ON v.id = c.document_version_id
          JOIN app.document AS d
            ON d.id_documento = v.id_documento
           AND d.active_version_id = v.id
         WHERE d.invalidated_at IS NULL
           AND v.processing_state = 'LISTA'
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW app.active_document_chunk")
    op.drop_index("ix_chunk_term_lexeme_fragment", table_name="chunk_term", schema="app")
    op.drop_table("chunk_term", schema="app")
    op.execute("DROP INDEX app.ix_document_chunk_embedding_hnsw")
    op.drop_index("ix_document_chunk_version", table_name="document_chunk", schema="app")
    op.drop_table("document_chunk", schema="app")
    op.drop_table("ocr_confidence_sample", schema="app")
    op.drop_index("uq_ocr_run_final_version", table_name="ocr_run", schema="app")
    op.drop_table("ocr_run", schema="app")
    op.drop_constraint("fk_document_active_version", "document", schema="app", type_="foreignkey")
    op.drop_index("ix_document_version_state", table_name="document_version", schema="app")
    op.drop_table("document_version", schema="app")
    op.drop_table("document", schema="app")
