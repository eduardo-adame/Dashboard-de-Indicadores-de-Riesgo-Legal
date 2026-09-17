"""Endurece los privilegios de las identidades de base de datos.

Revision ID: 0006_security_priv
Revises: 0005_security_audit_grants
"""
from collections.abc import Sequence

from alembic import op


revision: str = "0006_security_priv"
down_revision: str | None = "0005_security_audit_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MIGRATION_OWNER_ROLE = "riesgo_legal_migration_owner"
RUNTIME_ROLE = "riesgo_legal_runtime"
AUDIT_READER_ROLE = "riesgo_legal_audit_reader"


def _create_group_role(role: str) -> None:
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


def upgrade() -> None:
    """Materializa la mínima separación comprobable de privilegios."""
    _create_group_role(MIGRATION_OWNER_ROLE)
    op.execute(f"GRANT USAGE ON SCHEMA app, audit TO {MIGRATION_OWNER_ROLE}")
    op.execute(f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA app, audit TO {MIGRATION_OWNER_ROLE}")
    op.execute(f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA app, audit TO {MIGRATION_OWNER_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON app.role FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON app.permission FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON app.role_permission FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT ON app.role, app.permission, app.role_permission TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON audit.event, audit.event_resource FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT INSERT ON audit.event, audit.event_resource TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON audit.event, audit.event_resource FROM {AUDIT_READER_ROLE}")
    op.execute(f"GRANT SELECT ON audit.event, audit.event_resource TO {AUDIT_READER_ROLE}")


def downgrade() -> None:
    """Restaura la matriz amplia previa en bases desechables."""
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON app.role, app.permission, app.role_permission TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON audit.event, audit.event_resource FROM {RUNTIME_ROLE}")
    op.execute(f"GRANT INSERT ON audit.event, audit.event_resource TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON audit.event, audit.event_resource FROM {AUDIT_READER_ROLE}")
    op.execute(f"GRANT SELECT ON audit.event, audit.event_resource TO {AUDIT_READER_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA app, audit FROM {MIGRATION_OWNER_ROLE}")
    op.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA app, audit FROM {MIGRATION_OWNER_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA app, audit FROM {MIGRATION_OWNER_ROLE}")
    op.execute(
        f"""
        DO $role$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{MIGRATION_OWNER_ROLE}') THEN
            DROP ROLE {MIGRATION_OWNER_ROLE};
          END IF;
        END
        $role$;
        """
    )
