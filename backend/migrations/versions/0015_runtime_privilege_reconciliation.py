"""Reconcilia el rol runtime limitado con las tablas de la aplicación.

ID de revisión: 0015_runtime_privs
Revisa: 0014_rag_index_certificate
"""
from collections.abc import Sequence

from alembic import op


revision: str = "0015_runtime_privs"
down_revision: str | None = "0014_rag_index_certificate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "riesgo_legal_runtime"


def upgrade() -> None:
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON "
        "app.coordination_dispatch, app.document_candidate "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON app.document_candidate_page "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        "GRANT SELECT, INSERT ON "
        "app.proactive_input_snapshot, app.proactive_evaluation "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        "REVOKE UPDATE ON "
        "app.rag_operation, app.rag_final_fragment, app.context_reference "
        f"FROM {RUNTIME_ROLE}"
    )
    op.execute(
        f"GRANT SELECT ON public.alembic_version TO {RUNTIME_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"REVOKE SELECT ON public.alembic_version FROM {RUNTIME_ROLE}"
    )
    op.execute(
        "GRANT UPDATE ON "
        "app.rag_operation, app.rag_final_fragment, app.context_reference "
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(
        "REVOKE SELECT, INSERT ON "
        "app.proactive_input_snapshot, app.proactive_evaluation "
        f"FROM {RUNTIME_ROLE}"
    )
    op.execute(
        "REVOKE SELECT, INSERT, DELETE ON app.document_candidate_page "
        f"FROM {RUNTIME_ROLE}"
    )
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE ON "
        "app.coordination_dispatch, app.document_candidate "
        f"FROM {RUNTIME_ROLE}"
    )
