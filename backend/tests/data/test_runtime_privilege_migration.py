"""Evidencia PostgreSQL aislada de la revisión 0015 y su downgrade exclusivo de ACL."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url


BACKEND_ROOT = Path(__file__).resolve().parents[2]
ROLE = "riesgo_legal_runtime"


def _config(url: str) -> Config:
    parsed = make_url(url)
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    os.environ.update(
        POSTGRES_HOST=parsed.host or "localhost",
        POSTGRES_PORT=str(parsed.port or 5432),
        POSTGRES_USER=parsed.username or "",
        POSTGRES_PASSWORD=parsed.password or "",
        POSTGRES_DB=parsed.database or "",
    )
    return config


def _privileges(connection: sa.Connection, table: str) -> tuple[bool, bool, bool, bool]:
    return tuple(
        bool(connection.execute(sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
                                {"role": ROLE, "table": table, "privilege": privilege}).scalar_one())
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
    )


@pytest.mark.data_schema
def test_0015_source_contract() -> None:
    source = (BACKEND_ROOT / "migrations" / "versions" / "0015_runtime_privilege_reconciliation.py").read_text(encoding="utf-8")
    assert 'revision: str = "0015_runtime_privs"' in source
    assert 'down_revision: str | None = "0014_rag_index_certificate"' in source
    assert "CREATE ROLE" not in source and "DROP ROLE" not in source
    assert "ON ALL TABLES" not in source and "GRANT ALL" not in source


@pytest.mark.requires_db
@pytest.mark.data_schema
def test_0014_to_0015_acl_round_trip_and_reupgrade() -> None:
    source_url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not source_url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(source_url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {"riesgo_legal", "riesgo_legal_test"}:
        pytest.fail("a dedicated test administrator database is required")
    database_name = f"sec_rag_priv_test_{uuid4().hex}"
    admin = sa.create_engine(source_url, isolation_level="AUTOCOMMIT")
    isolated_url = parsed.set(database=database_name).render_as_string(hide_password=False)
    engine = None
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{database_name}"'))
        engine = sa.create_engine(isolated_url)
        config = _config(isolated_url)
        command.upgrade(config, "0014_rag_index_certificate")
        tables = (
            "app.coordination_dispatch", "app.document_candidate", "app.document_candidate_page",
            "app.proactive_input_snapshot", "app.proactive_evaluation", "app.rag_operation",
            "app.rag_final_fragment", "app.context_reference", "app.document_index_certificate",
            "app.document_chunk_index_receipt", "public.alembic_version",
        )
        with engine.connect() as connection:
            before = {table: _privileges(connection, table) for table in tables}
            roles_before = connection.execute(sa.text("SELECT rolname FROM pg_roles WHERE rolname IN "
                                                       "('riesgo_legal_runtime','riesgo_legal_migration_owner') ORDER BY rolname")).all()
            audit_before = connection.execute(sa.text("SELECT count(*) FROM audit.event")).scalar_one()
            assert before["public.alembic_version"] == (False, False, False, False)
            assert before["app.document_index_certificate"] == (True, True, False, False)
        command.upgrade(config, "0015_runtime_privs")
        with engine.connect() as connection:
            expected = {
                "app.coordination_dispatch": (True, True, True, False),
                "app.document_candidate": (True, True, True, False),
                "app.document_candidate_page": (True, True, False, True),
                "app.proactive_input_snapshot": (True, True, False, False),
                "app.proactive_evaluation": (True, True, False, False),
                "app.rag_operation": (True, True, False, False),
                "app.rag_final_fragment": (True, True, False, False),
                "app.context_reference": (True, True, False, False),
                "app.document_index_certificate": (True, True, False, False),
                "app.document_chunk_index_receipt": (True, True, False, False),
                "public.alembic_version": (True, False, False, False),
            }
            assert {table: _privileges(connection, table) for table in tables} == expected
            assert connection.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalar_one() == "0015_runtime_privs"
        command.downgrade(config, "0014_rag_index_certificate")
        with engine.connect() as connection:
            assert {table: _privileges(connection, table) for table in tables} == before
            assert connection.execute(sa.text("SELECT rolname FROM pg_roles WHERE rolname IN "
                                              "('riesgo_legal_runtime','riesgo_legal_migration_owner') ORDER BY rolname")).all() == roles_before
            assert connection.execute(sa.text("SELECT count(*) FROM audit.event")).scalar_one() == audit_before
        command.upgrade(config, "0015_runtime_privs")
        with engine.connect() as connection:
            assert {table: _privileges(connection, table) for table in tables} == expected
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        admin.dispose()
