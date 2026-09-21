"""Registra clasificación técnica y rechazos de archivos de ingesta.

Revision ID: 0007_ingestion_rejections
Revises: 0006_security_priv
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0007_ingestion_rejections"
down_revision: str | None = "0006_security_priv"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_ingest_file_format", "ingest_file", schema="app", type_="check")
    op.alter_column("ingest_file", "exchange_format", schema="app", existing_type=sa.Text(), nullable=True)
    op.add_column("ingest_file", sa.Column("declared_extension", sa.Text(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("detected_format", sa.Text(), nullable=True), schema="app")
    op.add_column(
        "ingest_file",
        sa.Column("format_classification", sa.Text(), nullable=False, server_default="SUPPORTED"),
        schema="app",
    )
    op.add_column(
        "ingest_file",
        sa.Column("technical_result", sa.Text(), nullable=False, server_default="ACCEPTED"),
        schema="app",
    )
    op.add_column("ingest_file", sa.Column("safe_cause_code", sa.Text(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("declared_name", sa.Text(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("source_locator", sa.Text(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("source_revision", sa.Integer(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("actor_identifier", sa.Text(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("idempotency_key", sa.Text(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("content_sha256", sa.LargeBinary(), nullable=True), schema="app")
    op.add_column("ingest_file", sa.Column("extraction_metadata", postgresql.JSONB(), nullable=True), schema="app")
    op.add_column("source_record", sa.Column("source_sheet", sa.Text(), nullable=True), schema="app")
    op.execute("UPDATE app.source_record SET source_sheet = 'LEGACY' WHERE source_sheet IS NULL")
    op.alter_column("source_record", "source_sheet", schema="app", nullable=False)
    op.drop_constraint("uq_source_record_file_row", "source_record", schema="app", type_="unique")
    op.create_unique_constraint(
        "uq_source_record_file_sheet_row",
        "source_record",
        ["ingest_file_id", "source_sheet", "row_number"],
        schema="app",
    )

    op.execute(
        """UPDATE app.ingest_file
           SET declared_extension = lower(exchange_format),
               detected_format = exchange_format,
               technical_result = CASE
                 WHEN state IN ('RECHAZADO', 'FALLIDO') THEN 'REJECTED'
                 ELSE 'ACCEPTED'
               END,
               safe_cause_code = CASE
                 WHEN state IN ('RECHAZADO', 'FALLIDO') THEN 'LEGACY_REJECTION'
                 ELSE NULL
               END,
               declared_name = stored.original_name,
               source_locator = stored.locator,
               source_revision = 1,
               actor_identifier = 'legacy',
               content_sha256 = stored.sha256
          FROM app.stored_object AS stored
         WHERE stored.id = app.ingest_file.stored_object_id"""
    )
    for column in (
        "declared_extension",
        "detected_format",
        "declared_name",
        "source_locator",
        "source_revision",
        "actor_identifier",
        "content_sha256",
    ):
        op.alter_column("ingest_file", column, schema="app", nullable=False)

    op.create_check_constraint(
        "ck_ingest_file_format",
        "ingest_file",
        "exchange_format IS NULL OR exchange_format IN ('CSV', 'XLSX', 'PDF', 'DOCX')",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_format_classification",
        "ingest_file",
        "format_classification IN ('SUPPORTED', 'UNSUPPORTED')",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_technical_result",
        "ingest_file",
        "technical_result IN ('ACCEPTED', 'REJECTED', 'FAILED')",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_supported_format",
        "ingest_file",
        "(format_classification = 'SUPPORTED' AND exchange_format IS NOT NULL) OR "
        "(format_classification = 'UNSUPPORTED' AND exchange_format IS NULL)",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_unsupported_terminal",
        "ingest_file",
        "format_classification = 'SUPPORTED' OR "
        "(state IN ('RECHAZADO', 'FALLIDO') AND technical_result IN ('REJECTED', 'FAILED'))",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_cause",
        "ingest_file",
        "(technical_result = 'ACCEPTED' AND safe_cause_code IS NULL) OR "
        "(technical_result <> 'ACCEPTED' AND safe_cause_code IS NOT NULL AND safe_cause_code <> '')",
        schema="app",
    )
    op.create_check_constraint("ck_ingest_file_declared_name", "ingest_file", "declared_name <> ''", schema="app")
    op.create_check_constraint("ck_ingest_file_source_locator", "ingest_file", "source_locator <> ''", schema="app")
    op.create_check_constraint("ck_ingest_file_source_revision", "ingest_file", "source_revision > 0", schema="app")
    op.create_check_constraint("ck_ingest_file_actor", "ingest_file", "actor_identifier <> ''", schema="app")
    op.create_check_constraint("ck_ingest_file_content_sha256", "ingest_file", "octet_length(content_sha256) = 32", schema="app")
    op.create_check_constraint(
        "ck_ingest_file_idempotency_key",
        "ingest_file",
        "idempotency_key IS NULL OR idempotency_key <> ''",
        schema="app",
    )
    op.create_index(
        "ix_ingest_file_format_result",
        "ingest_file",
        ["format_classification", "technical_result"],
        schema="app",
    )
    op.create_index(
        "uq_ingest_file_source_revision",
        "ingest_file",
        ["source_locator", "source_revision"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "uq_ingest_file_source_content",
        "ingest_file",
        ["source_locator", "content_sha256"],
        unique=True,
        schema="app",
    )
    op.create_index(
        "uq_ingest_file_manual_key",
        "ingest_file",
        ["actor_identifier", "idempotency_key"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_constraint("uq_source_record_file_sheet_row", "source_record", schema="app", type_="unique")
    op.execute(
        """WITH renumbered AS (
             SELECT id,
                    row_number() OVER (
                      PARTITION BY ingest_file_id
                      ORDER BY source_sheet, row_number NULLS LAST, id
                    ) AS legacy_row_number
             FROM app.source_record
           )
           UPDATE app.source_record AS target
              SET row_number = renumbered.legacy_row_number
             FROM renumbered
            WHERE target.id = renumbered.id"""
    )
    op.create_unique_constraint(
        "uq_source_record_file_row",
        "source_record",
        ["ingest_file_id", "row_number"],
        schema="app",
    )
    op.drop_index("uq_ingest_file_manual_key", table_name="ingest_file", schema="app")
    op.drop_index("uq_ingest_file_source_content", table_name="ingest_file", schema="app")
    op.drop_index("uq_ingest_file_source_revision", table_name="ingest_file", schema="app")
    op.drop_index("ix_ingest_file_format_result", table_name="ingest_file", schema="app")
    for constraint in (
        "ck_ingest_file_cause",
        "ck_ingest_file_unsupported_terminal",
        "ck_ingest_file_supported_format",
        "ck_ingest_file_technical_result",
        "ck_ingest_file_format_classification",
        "ck_ingest_file_format",
        "ck_ingest_file_idempotency_key",
        "ck_ingest_file_content_sha256",
        "ck_ingest_file_actor",
        "ck_ingest_file_source_revision",
        "ck_ingest_file_source_locator",
        "ck_ingest_file_declared_name",
    ):
        op.drop_constraint(constraint, "ingest_file", schema="app", type_="check")
    # La revisión anterior no puede representar formatos no soportados. El
    # downgrade se admite únicamente en bases desechables y retira esas filas
    # junto con cualquier trazabilidad técnica dependiente antes de restaurar
    # la nulabilidad original.
    op.execute(
        """DELETE FROM app.quarantine_transition
           WHERE quarantine_item_id IN (
             SELECT id FROM app.quarantine_item
             WHERE ingest_file_id IN (SELECT id FROM app.ingest_file WHERE exchange_format IS NULL)
           )"""
    )
    op.execute(
        """DELETE FROM app.quarantine_item
           WHERE ingest_file_id IN (SELECT id FROM app.ingest_file WHERE exchange_format IS NULL)"""
    )
    op.execute(
        """DELETE FROM app.record_application
           WHERE source_record_id IN (
             SELECT id FROM app.source_record
             WHERE ingest_file_id IN (SELECT id FROM app.ingest_file WHERE exchange_format IS NULL)
           )"""
    )
    op.execute(
        """DELETE FROM app.source_record
           WHERE ingest_file_id IN (SELECT id FROM app.ingest_file WHERE exchange_format IS NULL)"""
    )
    op.execute("DELETE FROM app.ingest_file WHERE exchange_format IS NULL")
    for column in (
        "safe_cause_code",
        "technical_result",
        "format_classification",
        "detected_format",
        "declared_extension",
        "extraction_metadata",
        "content_sha256",
        "idempotency_key",
        "actor_identifier",
        "source_revision",
        "source_locator",
        "declared_name",
    ):
        op.drop_column("ingest_file", column, schema="app")
    op.drop_column("source_record", "source_sheet", schema="app")
    op.alter_column("ingest_file", "exchange_format", schema="app", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_ingest_file_format",
        "ingest_file",
        "exchange_format IN ('CSV', 'XLSX', 'PDF', 'DOCX')",
        schema="app",
    )
