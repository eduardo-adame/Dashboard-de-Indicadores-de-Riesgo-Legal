"""Pruebas PostgreSQL reales del aprovisionamiento del LOGIN limitado por el operador."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from sqlalchemy.engine import make_url

from scripts.provision_runtime_role import ProvisioningError, provision


def _owner_connection() -> psycopg.Connection:
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {"riesgo_legal", "riesgo_legal_test"}:
        pytest.fail("a dedicated Security/runtime test database is required")
    return psycopg.connect(host=parsed.host, port=parsed.port or 5432,
                           dbname=parsed.database, user=parsed.username,
                           password=parsed.password, autocommit=True)


@pytest.mark.requires_db
def test_limited_login_is_idempotent_and_inherits_group(capsys) -> None:
    login = f"rl_test_{uuid4().hex}"
    password = secrets.token_urlsafe(32)
    with _owner_connection() as connection:
        try:
            provision(connection, login=login, password=password)
            provision(connection, login=login, password=password)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                    "FROM pg_roles WHERE rolname = %s", (login,),
                )
                assert cursor.fetchone() == (True, True, False, False, False, False)
                cursor.execute("SELECT pg_has_role(%s, 'riesgo_legal_runtime', 'USAGE')", (login,))
                assert cursor.fetchone() == (True,)
            assert capsys.readouterr().out == ""
        finally:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))


@pytest.mark.requires_db
def test_existing_elevated_login_fails_closed() -> None:
    login = f"rl_test_{uuid4().hex}"
    with _owner_connection() as connection:
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE ROLE {} LOGIN CREATEDB").format(sql.Identifier(login)))
            with pytest.raises(ProvisioningError):
                provision(connection, login=login, password=secrets.token_urlsafe(32))
        finally:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))


def test_provisioner_does_not_reconcile_application_table_grants() -> None:
    source = (Path(__file__).resolve().parents[2] / "scripts" / "provision_runtime_role.py").read_text(encoding="utf-8")
    assert "ON app." not in source
    assert "ON audit." not in source
    assert "GRANT ALL" not in source
