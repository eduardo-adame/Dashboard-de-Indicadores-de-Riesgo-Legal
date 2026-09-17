"""Seguridad persistible, auditoría append-only y separación de privilegios.

Los nombres de roles y la matriz SQL son configurables. Las capacidades
verificables de separación de privilegios son una restricción física.

Revision ID: 0005_security_audit_grants
Revises: 0004_analytics_rag_jobs
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_security_audit_grants"
down_revision: str | None = "0004_analytics_rag_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)

RUNTIME_ROLE = "riesgo_legal_runtime"
AUDIT_READER_ROLE = "riesgo_legal_audit_reader"


def _create_proposed_database_roles() -> None:
    for role in (RUNTIME_ROLE, AUDIT_READER_ROLE):
        op.execute(
            f"""
            DO $role$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
              END IF;
            END
            $role$;
            """
        )


def _seed_rbac() -> None:
    role_table = sa.table("role", sa.column("id", sa.Text()), sa.column("name", sa.Text()), sa.column("active", sa.Boolean()), schema="app")
    permission_table = sa.table("permission", sa.column("id", sa.Text()), schema="app")
    role_permission_table = sa.table("role_permission", sa.column("role_id", sa.Text()), sa.column("permission_id", sa.Text()), schema="app")

    roles = {
        "JURIDICO": ("Jurídico", ("dashboard.read", "kpi.read", "document.query")),
        "ANALISTA": (
            "Analista",
            (
                "dashboard.read", "kpi.read", "document.query", "ingest.upload", "ingest.execute",
                "quarantine.read", "quarantine.reinject", "quarantine.discard", "audit.read.own",
            ),
        ),
        "TI": (
            "TI",
            (
                "dashboard.read", "kpi.read", "document.query", "ingest.upload", "ingest.execute",
                "quarantine.read", "quarantine.reinject", "quarantine.discard", "audit.read.own",
                "audit.read.all", "user.create", "user.update", "user.disable", "user.reset_access",
                "role.assign", "document_acl.manage", "technical_config.manage",
            ),
        ),
    }
    permissions = sorted({permission for _, role_permissions in roles.values() for permission in role_permissions})
    op.bulk_insert(role_table, [{"id": role_id, "name": name, "active": True} for role_id, (name, _) in roles.items()])
    op.bulk_insert(permission_table, [{"id": permission} for permission in permissions])
    op.bulk_insert(
        role_permission_table,
        [
            {"role_id": role_id, "permission_id": permission}
            for role_id, (_, role_permissions) in roles.items()
            for permission in role_permissions
        ],
    )


def upgrade() -> None:
    op.create_table(
        "user_account",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("authorization_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("username <> ''", name="ck_user_account_username_nonempty"),
        sa.CheckConstraint("display_name <> ''", name="ck_user_account_display_name_nonempty"),
        sa.CheckConstraint("password_hash <> ''", name="ck_user_account_password_hash_nonempty"),
        sa.CheckConstraint("state IN ('ACTIVE', 'DISABLED')", name="ck_user_account_state"),
        sa.CheckConstraint("authorization_version > 0", name="ck_user_account_authorization_version"),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND disabled_at IS NULL) OR (state = 'DISABLED' AND disabled_at IS NOT NULL)",
            name="ck_user_account_disabled_state",
        ),
        sa.UniqueConstraint("username", name="uq_user_account_username"),
        schema="app",
    )

    op.create_table(
        "role",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("id IN ('JURIDICO', 'ANALISTA', 'TI')", name="ck_role_mvp"),
        sa.UniqueConstraint("name", name="uq_role_name"),
        schema="app",
    )
    op.create_table(
        "permission",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.CheckConstraint("id <> ''", name="ck_permission_id_nonempty"),
        schema="app",
    )
    op.create_table(
        "role_permission",
        sa.Column("role_id", sa.Text(), sa.ForeignKey("app.role.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("permission_id", sa.Text(), sa.ForeignKey("app.permission.id", ondelete="RESTRICT"), primary_key=True),
        schema="app",
    )
    op.create_table(
        "user_role",
        sa.Column("user_id", UUID, sa.ForeignKey("app.user_account.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("role_id", sa.Text(), sa.ForeignKey("app.role.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("(active AND revoked_at IS NULL) OR (NOT active AND revoked_at IS NOT NULL)", name="ck_user_role_active_state"),
        schema="app",
    )

    op.create_table(
        "access_session",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("app.user_account.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("refresh_token_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("authorization_version", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", UUID, nullable=True),
        sa.CheckConstraint("octet_length(refresh_token_sha256) = 32", name="ck_access_session_token_hash"),
        sa.CheckConstraint("authorization_version > 0", name="ck_access_session_authorization_version"),
        sa.CheckConstraint("state IN ('ACTIVE', 'LOGGED_OUT', 'INVALIDATED', 'EXPIRED')", name="ck_access_session_state"),
        sa.CheckConstraint("expires_at > started_at", name="ck_access_session_expiry"),
        sa.CheckConstraint(
            "(state = 'ACTIVE' AND invalidated_at IS NULL) OR (state <> 'ACTIVE' AND invalidated_at IS NOT NULL)",
            name="ck_access_session_invalidation",
        ),
        schema="app",
    )
    op.create_index("ix_access_session_user_state", "access_session", ["user_id", "state"], schema="app")

    op.create_table(
        "document_scope_grant",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("role_id", sa.Text(), sa.ForeignKey("app.role.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_family", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "source_family IN ('CONTRATOS_DOCUMENTOS', 'LITIGIOS', 'CUMPLIMIENTO', 'AUDITORIA_INTERNA')",
            name="ck_document_scope_grant_family",
        ),
        sa.UniqueConstraint("role_id", "source_family", name="uq_document_scope_grant_role_family"),
        schema="app",
    )
    op.create_table(
        "document_exception",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("app.user_account.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("role_id", sa.Text(), sa.ForeignKey("app.role.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("id_documento", sa.Text(), sa.ForeignKey("app.document.id_documento", ondelete="RESTRICT"), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("num_nonnulls(user_id, role_id) = 1", name="ck_document_exception_one_subject"),
        sa.CheckConstraint("decision IN ('ALLOW', 'DENY')", name="ck_document_exception_decision"),
        sa.CheckConstraint("(active AND revoked_at IS NULL) OR (NOT active AND revoked_at IS NOT NULL)", name="ck_document_exception_active_state"),
        schema="app",
    )
    op.create_index(
        "uq_document_exception_active_user",
        "document_exception",
        ["user_id", "id_documento"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("active AND user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_document_exception_active_role",
        "document_exception",
        ["role_id", "id_documento"],
        unique=True,
        schema="app",
        postgresql_where=sa.text("active AND role_id IS NOT NULL"),
    )

    op.create_foreign_key(
        "fk_rag_operation_user",
        "rag_operation",
        "user_account",
        ["user_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_rag_operation_session",
        "rag_operation",
        "access_session",
        ["session_id"],
        ["id"],
        source_schema="app",
        referent_schema="app",
        ondelete="RESTRICT",
    )

    op.create_table(
        "event",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_identifier", sa.Text(), nullable=False),
        sa.Column("actor_user_id", UUID, sa.ForeignKey("app.user_account.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_identifier", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("safe_cause_code", sa.Text(), nullable=True),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column("correlation_id", UUID, nullable=False),
        sa.Column("query_sha256", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint("actor_type IN ('HUMAN', 'PROCESS', 'ANONYMOUS')", name="ck_audit_event_actor_type"),
        sa.CheckConstraint("actor_identifier <> ''", name="ck_audit_event_actor_identifier"),
        sa.CheckConstraint("action <> ''", name="ck_audit_event_action"),
        sa.CheckConstraint("resource_type <> ''", name="ck_audit_event_resource_type"),
        sa.CheckConstraint("query_sha256 IS NULL OR octet_length(query_sha256) = 32", name="ck_audit_event_query_hash"),
        sa.UniqueConstraint("operation_id", "id", name="uq_audit_event_operation_id"),
        schema="audit",
    )
    op.create_index("ix_audit_event_time", "event", ["occurred_at"], schema="audit")
    op.create_index("ix_audit_event_actor", "event", ["actor_identifier", "occurred_at"], schema="audit")
    op.create_index("ix_audit_event_correlation", "event", ["correlation_id"], schema="audit")

    op.create_table(
        "event_resource",
        sa.Column("event_id", UUID, sa.ForeignKey("audit.event.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("resource_type", sa.Text(), primary_key=True),
        sa.Column("resource_identifier", sa.Text(), primary_key=True),
        sa.Column("id_documento", sa.Text(), sa.ForeignKey("app.document.id_documento", ondelete="RESTRICT"), nullable=True),
        sa.Column("fragment_id", UUID, sa.ForeignKey("app.document_chunk.id", ondelete="RESTRICT"), nullable=True),
        sa.CheckConstraint("resource_type <> ''", name="ck_audit_event_resource_type_nonempty"),
        sa.CheckConstraint("resource_identifier <> ''", name="ck_audit_event_resource_id_nonempty"),
        sa.CheckConstraint(
            "fragment_id IS NULL OR id_documento IS NOT NULL",
            name="ck_audit_event_fragment_has_document",
        ),
        schema="audit",
    )
    op.create_index("ix_audit_event_resource_document", "event_resource", ["id_documento", "fragment_id"], schema="audit")

    _seed_rbac()
    _create_proposed_database_roles()

    op.execute(f"GRANT USAGE ON SCHEMA app, audit TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA app TO {RUNTIME_ROLE}")
    op.execute(f"GRANT INSERT ON audit.event, audit.event_resource TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE UPDATE, DELETE ON audit.event, audit.event_resource FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA audit TO {AUDIT_READER_ROLE}")
    op.execute(f"GRANT SELECT ON audit.event, audit.event_resource TO {AUDIT_READER_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE ON audit.event, audit.event_resource FROM {AUDIT_READER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA app FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA audit FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA audit FROM {AUDIT_READER_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA app, audit FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA audit FROM {AUDIT_READER_ROLE}")

    op.drop_index("ix_audit_event_resource_document", table_name="event_resource", schema="audit")
    op.drop_table("event_resource", schema="audit")
    op.drop_index("ix_audit_event_correlation", table_name="event", schema="audit")
    op.drop_index("ix_audit_event_actor", table_name="event", schema="audit")
    op.drop_index("ix_audit_event_time", table_name="event", schema="audit")
    op.drop_table("event", schema="audit")

    op.drop_constraint("fk_rag_operation_session", "rag_operation", schema="app", type_="foreignkey")
    op.drop_constraint("fk_rag_operation_user", "rag_operation", schema="app", type_="foreignkey")
    op.drop_index("uq_document_exception_active_role", table_name="document_exception", schema="app")
    op.drop_index("uq_document_exception_active_user", table_name="document_exception", schema="app")
    op.drop_table("document_exception", schema="app")
    op.drop_table("document_scope_grant", schema="app")
    op.drop_index("ix_access_session_user_state", table_name="access_session", schema="app")
    op.drop_table("access_session", schema="app")
    op.drop_table("user_role", schema="app")
    op.drop_table("role_permission", schema="app")
    op.drop_table("permission", schema="app")
    op.drop_table("role", schema="app")
    op.drop_table("user_account", schema="app")

    for role in (RUNTIME_ROLE, AUDIT_READER_ROLE):
        op.execute(
            f"""
            DO $role$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                DROP ROLE {role};
              END IF;
            END
            $role$;
            """
        )
