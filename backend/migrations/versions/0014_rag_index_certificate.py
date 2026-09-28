"""Certificación durable de versiones documentales completamente indexadas.

Revision ID: 0014_rag_index_certificate
Revises: 0013_proactive_analysis
"""
from collections.abc import Sequence

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0014_rag_index_certificate"
down_revision: str | None = "0013_proactive_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
CONTRACT = "bge-m3:d1024:c512:o50:lex-nfkc-casefold-alnum-v1"


def upgrade() -> None:
    op.create_table(
        "document_index_certificate",
        sa.Column(
            "document_version_id", UUID,
            sa.ForeignKey("app.document_version.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("index_contract_version", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=False),
        sa.Column("chunk_size_tokens", sa.Integer(), nullable=False),
        sa.Column("chunk_overlap_tokens", sa.Integer(), nullable=False),
        sa.Column("expected_chunk_count", sa.Integer(), nullable=False),
        sa.Column("embedded_chunk_count", sa.Integer(), nullable=False),
        sa.Column("lexical_processed_chunk_count", sa.Integer(), nullable=False),
        sa.Column("zero_lexeme_chunk_count", sa.Integer(), nullable=False),
        sa.Column("source_text_sha256", sa.LargeBinary(), nullable=False),
        sa.Column(
            "certified_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            f"index_contract_version = '{CONTRACT}'",
            name="ck_document_index_certificate_contract",
        ),
        sa.CheckConstraint(
            "embedding_model = 'BAAI/bge-m3' AND embedding_dimensions = 1024 "
            "AND chunk_size_tokens = 512 AND chunk_overlap_tokens = 50",
            name="ck_document_index_certificate_configuration",
        ),
        sa.CheckConstraint(
            "expected_chunk_count > 0 "
            "AND embedded_chunk_count = expected_chunk_count "
            "AND lexical_processed_chunk_count = expected_chunk_count "
            "AND zero_lexeme_chunk_count BETWEEN 0 AND lexical_processed_chunk_count",
            name="ck_document_index_certificate_counts",
        ),
        sa.CheckConstraint(
            "octet_length(source_text_sha256) = 32",
            name="ck_document_index_certificate_source_hash",
        ),
        schema="app",
    )
    op.create_table(
        "document_chunk_index_receipt",
        sa.Column(
            "fragment_id", UUID,
            sa.ForeignKey("app.document_chunk.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("index_contract_version", sa.Text(), nullable=False),
        sa.Column("lexical_term_count", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"index_contract_version = '{CONTRACT}'",
            name="ck_document_chunk_index_receipt_contract",
        ),
        sa.CheckConstraint(
            "lexical_term_count >= 0",
            name="ck_document_chunk_index_receipt_term_count",
        ),
        schema="app",
    )

    op.execute(
        """
        CREATE FUNCTION app.lock_document_index_version(version_id uuid)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'index certification requires read committed isolation'
                    USING ERRCODE = '25006';
            END IF;
            PERFORM 1 FROM app.document_version WHERE id = version_id FOR UPDATE;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.lock_document_index_fragment(fragment_id uuid)
        RETURNS uuid LANGUAGE plpgsql AS $$
        DECLARE
            version_id uuid;
        BEGIN
            SELECT document_version_id INTO version_id
              FROM app.document_chunk WHERE id = fragment_id FOR UPDATE;
            IF version_id IS NOT NULL THEN
                PERFORM app.lock_document_index_version(version_id);
            END IF;
            RETURN version_id;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.lock_document_index_certificate_insert()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM app.lock_document_index_version(NEW.document_version_id);
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_index_certificate_serialize
        BEFORE INSERT ON app.document_index_certificate
        FOR EACH ROW EXECUTE FUNCTION app.lock_document_index_certificate_insert()
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.validate_document_index_certificate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            source_text text;
            version_state text;
            chunk_total bigint;
            embedded_total bigint;
            first_ordinal integer;
            last_ordinal integer;
            receipt_total bigint;
            zero_lexeme_total bigint;
            invalid_receipts bigint;
        BEGIN
            PERFORM app.lock_document_index_version(NEW.document_version_id);
            SELECT consolidated_text, processing_state
              INTO source_text, version_state
              FROM app.document_version
             WHERE id = NEW.document_version_id;
            IF source_text IS NULL OR version_state <> 'LISTA'
               OR NEW.source_text_sha256 <> sha256(convert_to(source_text, 'UTF8')) THEN
                RAISE EXCEPTION 'certified version source or state is invalid'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_document_index_certificate_source';
            END IF;

            SELECT count(*),
                   count(*) FILTER (WHERE embedding IS NOT NULL AND vector_dims(embedding) = 1024),
                   min(ordinal), max(ordinal)
              INTO chunk_total, embedded_total, first_ordinal, last_ordinal
              FROM app.document_chunk
             WHERE document_version_id = NEW.document_version_id;

            IF chunk_total <> NEW.expected_chunk_count
               OR embedded_total <> NEW.embedded_chunk_count
               OR first_ordinal <> 0
               OR last_ordinal <> NEW.expected_chunk_count - 1 THEN
                RAISE EXCEPTION 'certified version has incomplete chunks or embeddings'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_document_index_certificate_chunks';
            END IF;

            SELECT count(r.fragment_id),
                   count(*) FILTER (WHERE r.lexical_term_count = 0),
                   count(*) FILTER (
                       WHERE r.fragment_id IS NULL
                          OR r.index_contract_version <> NEW.index_contract_version
                          OR r.lexical_term_count <> (
                              SELECT count(*) FROM app.chunk_term t
                               WHERE t.fragment_id = c.id
                          )
                   )
              INTO receipt_total, zero_lexeme_total, invalid_receipts
              FROM app.document_chunk c
              LEFT JOIN app.document_chunk_index_receipt r ON r.fragment_id = c.id
             WHERE c.document_version_id = NEW.document_version_id;

            IF receipt_total <> NEW.lexical_processed_chunk_count
               OR zero_lexeme_total <> NEW.zero_lexeme_chunk_count
               OR invalid_receipts <> 0 THEN
                RAISE EXCEPTION 'certified version has incomplete lexical indexing'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_document_index_certificate_lexical';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_document_index_certificate_complete
        AFTER INSERT ON app.document_index_certificate
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION app.validate_document_index_certificate()
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.prevent_document_index_certificate_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'index certificate is immutable'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'ck_document_index_certificate_immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_index_certificate_immutable
        BEFORE UPDATE OR DELETE ON app.document_index_certificate
        FOR EACH ROW EXECUTE FUNCTION app.prevent_document_index_certificate_mutation()
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.prevent_document_chunk_index_receipt_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            version_id uuid;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                version_id := app.lock_document_index_fragment(NEW.fragment_id);
            ELSE
                PERFORM app.lock_document_index_fragment(OLD.fragment_id);
                RAISE EXCEPTION 'index receipt is immutable'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_document_chunk_index_receipt_immutable';
            END IF;
            IF EXISTS (
                SELECT 1 FROM app.document_index_certificate
                 WHERE document_version_id = version_id
            ) THEN
                RAISE EXCEPTION 'cannot add receipt to certified version'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_document_chunk_index_receipt_certified';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_document_chunk_index_receipt_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON app.document_chunk_index_receipt
        FOR EACH ROW EXECUTE FUNCTION app.prevent_document_chunk_index_receipt_mutation()
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.prevent_certified_document_chunk_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM app.lock_document_index_version(NEW.document_version_id);
                IF EXISTS (
                    SELECT 1 FROM app.document_index_certificate
                     WHERE document_version_id = NEW.document_version_id
                ) THEN
                    RAISE EXCEPTION 'certified document chunks are immutable'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_certified_document_chunk_immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.document_version_id <> NEW.document_version_id THEN
                PERFORM app.lock_document_index_version(
                    LEAST(OLD.document_version_id, NEW.document_version_id)
                );
                PERFORM app.lock_document_index_version(
                    GREATEST(OLD.document_version_id, NEW.document_version_id)
                );
            ELSE
                PERFORM app.lock_document_index_version(OLD.document_version_id);
            END IF;
            IF EXISTS (
                SELECT 1 FROM app.document_index_certificate
                 WHERE document_version_id = OLD.document_version_id
            ) OR (TG_OP = 'UPDATE' AND EXISTS (
                SELECT 1 FROM app.document_index_certificate
                 WHERE document_version_id = NEW.document_version_id
            )) THEN
                RAISE EXCEPTION 'certified document chunks are immutable'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_certified_document_chunk_immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_certified_document_chunk_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON app.document_chunk
        FOR EACH ROW EXECUTE FUNCTION app.prevent_certified_document_chunk_mutation()
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.prevent_certified_chunk_term_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            old_version_id uuid;
            new_version_id uuid;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                PERFORM app.lock_document_index_fragment(NEW.fragment_id);
                IF EXISTS (
                    SELECT 1 FROM app.document_chunk c
                    JOIN app.document_index_certificate cert
                      ON cert.document_version_id = c.document_version_id
                    WHERE c.id = NEW.fragment_id
                ) THEN
                    RAISE EXCEPTION 'certified chunk terms are immutable'
                        USING ERRCODE = '23514',
                              CONSTRAINT = 'ck_certified_chunk_term_immutable';
                END IF;
                RETURN NEW;
            END IF;
            old_version_id := app.lock_document_index_fragment(OLD.fragment_id);
            IF TG_OP = 'UPDATE' AND OLD.fragment_id <> NEW.fragment_id THEN
                new_version_id := app.lock_document_index_fragment(NEW.fragment_id);
            END IF;
            IF EXISTS (
                SELECT 1 FROM app.document_chunk c
                JOIN app.document_index_certificate cert
                  ON cert.document_version_id = c.document_version_id
                WHERE c.id = OLD.fragment_id
            ) OR (TG_OP = 'UPDATE' AND EXISTS (
                SELECT 1 FROM app.document_chunk c
                JOIN app.document_index_certificate cert
                  ON cert.document_version_id = c.document_version_id
                WHERE c.id = NEW.fragment_id
            )) THEN
                RAISE EXCEPTION 'certified chunk terms are immutable'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_certified_chunk_term_immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_certified_chunk_term_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON app.chunk_term
        FOR EACH ROW EXECUTE FUNCTION app.prevent_certified_chunk_term_mutation()
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.prevent_certified_document_source_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM app.lock_document_index_version(OLD.id);
            IF EXISTS (
                SELECT 1 FROM app.document_index_certificate
                 WHERE document_version_id = OLD.id
            ) AND (
                OLD.id_documento IS DISTINCT FROM NEW.id_documento OR
                OLD.version_number IS DISTINCT FROM NEW.version_number OR
                OLD.stored_object_id IS DISTINCT FROM NEW.stored_object_id OR
                OLD.source_record_id IS DISTINCT FROM NEW.source_record_id OR
                OLD.native_text IS DISTINCT FROM NEW.native_text OR
                OLD.consolidated_text IS DISTINCT FROM NEW.consolidated_text OR
                OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
            ) THEN
                RAISE EXCEPTION 'certified document source is immutable'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'ck_certified_document_source_immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_certified_document_source_immutable
        BEFORE UPDATE ON app.document_version
        FOR EACH ROW EXECUTE FUNCTION app.prevent_certified_document_source_mutation()
        """
    )

    op.execute(
        "GRANT SELECT, INSERT ON app.document_index_certificate, "
        "app.document_chunk_index_receipt TO riesgo_legal_runtime"
    )


def downgrade() -> None:
    if context.is_offline_mode():
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM app.document_index_certificate) "
            "OR EXISTS (SELECT 1 FROM app.document_chunk_index_receipt) THEN "
            "RAISE EXCEPTION 'index evidence exists; downgrade would lose certification'; "
            "END IF; END $$"
        )
    else:
        bind = op.get_bind()
        has_evidence = bind.execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM app.document_index_certificate) "
                "OR EXISTS (SELECT 1 FROM app.document_chunk_index_receipt)"
            )
        ).scalar()
        if has_evidence:
            raise RuntimeError("index evidence exists; downgrade would lose certification")

    op.execute("DROP TRIGGER trg_certified_document_source_immutable ON app.document_version")
    op.execute("DROP FUNCTION app.prevent_certified_document_source_mutation()")
    op.execute("DROP TRIGGER trg_certified_chunk_term_immutable ON app.chunk_term")
    op.execute("DROP FUNCTION app.prevent_certified_chunk_term_mutation()")
    op.execute("DROP TRIGGER trg_certified_document_chunk_immutable ON app.document_chunk")
    op.execute("DROP FUNCTION app.prevent_certified_document_chunk_mutation()")
    op.execute("DROP TRIGGER trg_document_chunk_index_receipt_immutable ON app.document_chunk_index_receipt")
    op.execute("DROP FUNCTION app.prevent_document_chunk_index_receipt_mutation()")
    op.execute("DROP TRIGGER trg_document_index_certificate_immutable ON app.document_index_certificate")
    op.execute("DROP FUNCTION app.prevent_document_index_certificate_mutation()")
    op.execute("DROP TRIGGER trg_document_index_certificate_complete ON app.document_index_certificate")
    op.execute("DROP FUNCTION app.validate_document_index_certificate()")
    op.execute("DROP TRIGGER trg_document_index_certificate_serialize ON app.document_index_certificate")
    op.execute("DROP FUNCTION app.lock_document_index_certificate_insert()")
    op.execute("DROP FUNCTION app.lock_document_index_fragment(uuid)")
    op.execute("DROP FUNCTION app.lock_document_index_version(uuid)")
    op.drop_table("document_chunk_index_receipt", schema="app")
    op.drop_table("document_index_certificate", schema="app")
