"""Capacidad funcional de gestión/reproceso documental.

Introduce una capacidad dedicada para las operaciones administrativas de gestión
documental y la asigna a Analista y TI. La lectura documental (`document.query`)
permanece separada y no autoriza operaciones administrativas.

Revision ID: 0011_document_manage_capability
Revises: 0010_document_candidate
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0011_document_manage_capability"
down_revision: str | None = "0010_document_candidate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CAPABILITY = "document.manage"
ROLES = ("ANALISTA", "TI")


def upgrade() -> None:
    op.execute(f"DELETE FROM app.role_permission WHERE permission_id = '{CAPABILITY}'")
    op.execute(f"DELETE FROM app.permission WHERE id = '{CAPABILITY}'")
    op.execute(f"INSERT INTO app.permission (id) VALUES ('{CAPABILITY}')")
    values = ", ".join(f"('{role}', '{CAPABILITY}')" for role in ROLES)
    op.execute(f"INSERT INTO app.role_permission (role_id, permission_id) VALUES {values}")


def downgrade() -> None:
    op.execute(f"DELETE FROM app.role_permission WHERE permission_id = '{CAPABILITY}'")
    op.execute(f"DELETE FROM app.permission WHERE id = '{CAPABILITY}'")
