"""Ingesta, procedencia, cuarentena unificada y datos conformes.

Revision ID: 0002_ingestion_domain
Revises: 0001_foundation
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0002_ingestion_domain"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def upgrade() -> None:
    op.create_table(
        "ingest_file",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("stored_object_id", UUID, sa.ForeignKey("app.stored_object.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_family", sa.Text(), nullable=False),
        sa.Column("exchange_format", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "source_family IN ('CONTRATOS_DOCUMENTOS', 'LITIGIOS', 'CUMPLIMIENTO', 'AUDITORIA_INTERNA')",
            name="ck_ingest_file_family",
        ),
        sa.CheckConstraint("exchange_format IN ('CSV', 'XLSX', 'PDF', 'DOCX')", name="ck_ingest_file_format"),
        sa.CheckConstraint(
            "state IN ('RECIBIDO', 'PROCESANDO', 'CONFORME', 'RECHAZADO', 'CUARENTENA', 'COMPLETADO', 'FALLIDO')",
            name="ck_ingest_file_state",
        ),
        sa.UniqueConstraint("operation_id", name="uq_ingest_file_operation"),
        schema="app",
    )
    op.create_index("ix_ingest_file_correlation", "ingest_file", ["correlation_id"], schema="app")

    op.create_table(
        "source_record",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("ingest_file_id", UUID, sa.ForeignKey("app.ingest_file.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=True),
        sa.Column("raw_payload", JSONB, nullable=True),
        sa.Column("record_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("extraction_state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("row_number IS NULL OR row_number > 0", name="ck_source_record_row"),
        sa.CheckConstraint("octet_length(record_sha256) = 32", name="ck_source_record_sha256"),
        sa.CheckConstraint(
            "extraction_state IN ('EXTRAIDO', 'RECHAZADO', 'CUARENTENA')",
            name="ck_source_record_state",
        ),
        sa.UniqueConstraint("ingest_file_id", "row_number", name="uq_source_record_file_row"),
        schema="app",
    )
    op.create_index("ix_source_record_hash", "source_record", ["record_sha256"], schema="app")

    op.create_table(
        "record_application",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("business_id", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("business_id <> ''", name="ck_record_application_business_id"),
        sa.CheckConstraint("octet_length(payload_sha256) = 32", name="ck_record_application_sha256"),
        sa.CheckConstraint(
            "entity_type IN ('CONTRATO', 'LITIGIO', 'OBLIGACION', 'INCIDENTE', 'ASUNTO', 'DOCUMENTO')",
            name="ck_record_application_entity_type",
        ),
        sa.CheckConstraint(
            "result IN ('INCORPORADO', 'ACTUALIZADO', 'IDEMPOTENTE', 'CONFLICTO')",
            name="ck_record_application_result",
        ),
        sa.UniqueConstraint("source_record_id", "entity_type", "business_id", name="uq_record_application_target"),
        sa.UniqueConstraint("operation_id", "source_record_id", name="uq_record_application_operation"),
        schema="app",
    )
    op.create_index(
        "ix_record_application_business",
        "record_application",
        ["entity_type", "business_id"],
        schema="app",
    )

    op.create_table(
        "quarantine_item",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("ingest_file_id", UUID, sa.ForeignKey("app.ingest_file.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("business_id", sa.Text(), nullable=True),
        sa.Column("cause_code", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="Pendiente"),
        sa.Column("original_payload", JSONB, nullable=True),
        sa.Column("candidate_payload", JSONB, nullable=True),
        sa.Column("original_object_id", UUID, sa.ForeignKey("app.stored_object.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("candidate_object_id", UUID, sa.ForeignKey("app.stored_object.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("discard_justification", sa.Text(), nullable=True),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        *_timestamps(),
        sa.CheckConstraint("state IN ('Pendiente', 'Reinyectado', 'Descartado')", name="ck_quarantine_item_state"),
        sa.CheckConstraint(
            "original_payload IS NOT NULL OR original_object_id IS NOT NULL",
            name="ck_quarantine_item_original_preserved",
        ),
        sa.CheckConstraint(
            "cause_code <> 'IDENTITY_CONFLICT' OR (candidate_payload IS NOT NULL OR candidate_object_id IS NOT NULL)",
            name="ck_quarantine_identity_candidate_preserved",
        ),
        sa.CheckConstraint(
            "(state = 'Descartado' AND discard_justification IS NOT NULL AND discard_justification <> '') "
            "OR (state <> 'Descartado' AND discard_justification IS NULL)",
            name="ck_quarantine_discard_justification",
        ),
        sa.UniqueConstraint("operation_id", name="uq_quarantine_item_operation"),
        schema="app",
    )
    op.create_index("ix_quarantine_item_state", "quarantine_item", ["state"], schema="app")
    op.create_index(
        "ix_quarantine_item_business",
        "quarantine_item",
        ["entity_type", "business_id"],
        schema="app",
    )

    op.create_table(
        "quarantine_transition",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("quarantine_item_id", UUID, sa.ForeignKey("app.quarantine_item.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_identifier", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("from_state IS NULL OR from_state IN ('Pendiente', 'Reinyectado', 'Descartado')", name="ck_quarantine_transition_from"),
        sa.CheckConstraint("to_state IN ('Pendiente', 'Reinyectado', 'Descartado')", name="ck_quarantine_transition_to"),
        sa.CheckConstraint("actor_type IN ('HUMAN', 'PROCESS')", name="ck_quarantine_transition_actor"),
        sa.UniqueConstraint("operation_id", name="uq_quarantine_transition_operation"),
        schema="app",
    )

    op.create_table(
        "contract_record",
        sa.Column("id_contrato", sa.Text(), primary_key=True),
        sa.Column("fecha_solicitud", sa.Date(), nullable=False),
        sa.Column("fecha_firma", sa.Date(), nullable=True),
        sa.Column("fecha_vencimiento", sa.Date(), nullable=False),
        sa.Column("estado_revision", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("id_contrato <> ''", name="ck_contract_id_nonempty"),
        sa.CheckConstraint("fecha_firma IS NULL OR fecha_firma >= fecha_solicitud", name="ck_contract_signature_date"),
        sa.CheckConstraint(
            "estado_revision IN ('No iniciado', 'En revisión', 'En renovación', 'Renovado', 'Cancelado')",
            name="ck_contract_review_state",
        ),
        sa.CheckConstraint("octet_length(payload_sha256) = 32", name="ck_contract_sha256"),
        schema="app",
    )

    op.create_table(
        "litigation",
        sa.Column("id_litigio", sa.Text(), primary_key=True),
        sa.Column("fecha_apertura", sa.Date(), nullable=False),
        sa.Column("estado", sa.Text(), nullable=False),
        sa.Column("nivel_severidad", sa.Text(), nullable=False),
        sa.Column("monto_reclamado", sa.Numeric(), nullable=True),
        sa.Column("estimacion_interna", sa.Numeric(), nullable=True),
        sa.Column("id_contrato", sa.Text(), sa.ForeignKey("app.contract_record.id_contrato", ondelete="RESTRICT"), nullable=True),
        sa.Column("payload_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("id_litigio <> ''", name="ck_litigation_id_nonempty"),
        sa.CheckConstraint("estado <> ''", name="ck_litigation_state_nonempty"),
        sa.CheckConstraint("nivel_severidad IN ('alto', 'medio', 'bajo')", name="ck_litigation_severity"),
        sa.CheckConstraint("monto_reclamado IS NULL OR monto_reclamado >= 0", name="ck_litigation_claim_nonnegative"),
        sa.CheckConstraint("estimacion_interna IS NULL OR estimacion_interna >= 0", name="ck_litigation_estimate_nonnegative"),
        sa.CheckConstraint("octet_length(payload_sha256) = 32", name="ck_litigation_sha256"),
        schema="app",
    )

    op.create_table(
        "compliance_obligation",
        sa.Column("id_obligacion", sa.Text(), primary_key=True),
        sa.Column("fecha_limite", sa.Date(), nullable=False),
        sa.Column("evidencia_cumplimiento", sa.Text(), nullable=True),
        sa.Column("id_contrato", sa.Text(), sa.ForeignKey("app.contract_record.id_contrato", ondelete="RESTRICT"), nullable=True),
        sa.Column("payload_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("id_obligacion <> ''", name="ck_obligation_id_nonempty"),
        sa.CheckConstraint("octet_length(payload_sha256) = 32", name="ck_obligation_sha256"),
        schema="app",
    )

    op.create_table(
        "incident",
        sa.Column("id_incidente", sa.Text(), primary_key=True),
        sa.Column("fecha_evento", sa.Date(), nullable=False),
        sa.Column("area", sa.Text(), nullable=False),
        sa.Column("nivel_severidad", sa.Text(), nullable=False),
        sa.Column("id_obligacion", sa.Text(), sa.ForeignKey("app.compliance_obligation.id_obligacion", ondelete="RESTRICT"), nullable=True),
        sa.Column("payload_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("id_incidente <> ''", name="ck_incident_id_nonempty"),
        sa.CheckConstraint("area <> ''", name="ck_incident_area_nonempty"),
        sa.CheckConstraint("nivel_severidad IN ('alto', 'medio', 'bajo')", name="ck_incident_severity"),
        sa.CheckConstraint("octet_length(payload_sha256) = 32", name="ck_incident_sha256"),
        schema="app",
    )

    op.create_table(
        "legal_matter",
        sa.Column("id_asunto", sa.Text(), primary_key=True),
        sa.Column("tipo_asunto", sa.Text(), nullable=False),
        sa.Column("estado", sa.Text(), nullable=False),
        sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("source_entity_type", sa.Text(), nullable=False),
        sa.Column("source_business_id", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("source_record_id", UUID, sa.ForeignKey("app.source_record.id", ondelete="RESTRICT"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("id_asunto <> ''", name="ck_legal_matter_id_nonempty"),
        sa.CheckConstraint("tipo_asunto IN ('Contrato', 'Litigio', 'Cumplimiento', 'Auditoría')", name="ck_legal_matter_type"),
        sa.CheckConstraint("estado <> ''", name="ck_legal_matter_state_nonempty"),
        sa.CheckConstraint("source_business_id <> ''", name="ck_legal_matter_source_nonempty"),
        sa.CheckConstraint("octet_length(payload_sha256) = 32", name="ck_legal_matter_sha256"),
        schema="app",
    )


def downgrade() -> None:
    for table in (
        "legal_matter",
        "incident",
        "compliance_obligation",
        "litigation",
        "contract_record",
        "quarantine_transition",
        "quarantine_item",
        "record_application",
        "source_record",
        "ingest_file",
    ):
        op.drop_table(table, schema="app")
