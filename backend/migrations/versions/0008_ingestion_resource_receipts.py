"""Representa rechazos de recepción sin objeto ni hash completo.

Revision ID: 0008_ingestion_resource_receipts
Revises: 0007_ingestion_rejections
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0008_ingestion_resource_receipts"
down_revision: str | None = "0007_ingestion_rejections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_ingest_file_content_sha256", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_format_classification", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_supported_format", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_unsupported_terminal", "ingest_file", schema="app", type_="check")
    op.alter_column("ingest_file", "stored_object_id", schema="app", nullable=True)
    op.alter_column("ingest_file", "content_sha256", schema="app", nullable=True)
    op.add_column(
        "ingest_file",
        sa.Column("content_sha256_complete", sa.Boolean(), nullable=False, server_default=sa.true()),
        schema="app",
    )
    op.add_column("ingest_file", sa.Column("observed_byte_size", sa.BigInteger(), nullable=True), schema="app")
    op.create_check_constraint(
        "ck_ingest_file_content_sha256",
        "ingest_file",
        "(content_sha256_complete AND content_sha256 IS NOT NULL AND octet_length(content_sha256) = 32) OR "
        "(NOT content_sha256_complete AND content_sha256 IS NULL)",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_observed_byte_size",
        "ingest_file",
        "observed_byte_size IS NULL OR observed_byte_size >= 0",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_format_classification",
        "ingest_file",
        "format_classification IN ('SUPPORTED', 'UNSUPPORTED', 'UNDETERMINED')",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_supported_format",
        "ingest_file",
        "(format_classification = 'SUPPORTED' AND exchange_format IS NOT NULL) OR "
        "(format_classification IN ('UNSUPPORTED', 'UNDETERMINED') AND exchange_format IS NULL)",
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
        "ck_ingest_file_resource_rejection_object",
        "ingest_file",
        "stored_object_id IS NOT NULL OR "
        "(state = 'RECHAZADO' AND technical_result = 'REJECTED' "
        "AND safe_cause_code = 'RESOURCE_LIMIT_EXCEEDED' AND NOT content_sha256_complete)",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_incomplete_receipt",
        "ingest_file",
        "content_sha256_complete OR "
        "(format_classification = 'UNDETERMINED' AND state = 'RECHAZADO' "
        "AND technical_result = 'REJECTED' AND safe_cause_code = 'RESOURCE_LIMIT_EXCEEDED' "
        "AND stored_object_id IS NULL)",
        schema="app",
    )
    op.create_check_constraint(
        "ck_ingest_file_undetermined_resource_rejection",
        "ingest_file",
        "format_classification <> 'UNDETERMINED' OR "
        "(NOT content_sha256_complete AND state = 'RECHAZADO' "
        "AND technical_result = 'REJECTED' AND safe_cause_code = 'RESOURCE_LIMIT_EXCEEDED' "
        "AND stored_object_id IS NULL)",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ingest_file_undetermined_resource_rejection", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_incomplete_receipt", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_resource_rejection_object", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_unsupported_terminal", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_supported_format", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_format_classification", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_observed_byte_size", "ingest_file", schema="app", type_="check")
    op.drop_constraint("ck_ingest_file_content_sha256", "ingest_file", schema="app", type_="check")
    op.execute("DELETE FROM app.ingest_file WHERE NOT content_sha256_complete")
    op.drop_column("ingest_file", "observed_byte_size", schema="app")
    op.drop_column("ingest_file", "content_sha256_complete", schema="app")
    op.alter_column("ingest_file", "content_sha256", schema="app", nullable=False)
    op.alter_column("ingest_file", "stored_object_id", schema="app", nullable=False)
    op.create_check_constraint(
        "ck_ingest_file_content_sha256", "ingest_file", "octet_length(content_sha256) = 32", schema="app"
    )
    op.create_check_constraint(
        "ck_ingest_file_format_classification",
        "ingest_file",
        "format_classification IN ('SUPPORTED', 'UNSUPPORTED')",
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
