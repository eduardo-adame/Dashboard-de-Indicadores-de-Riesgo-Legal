"""Privilegios efectivos de un LOGIN real, nunca de un owner que usa SET ROLE."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
from uuid import uuid4

from alembic import command
from alembic.config import Config
import psycopg
from psycopg import sql
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.security.runtime_check import REQUIRED_PRIVILEGES, RuntimeCheckError, check_runtime_connection
from scripts.provision_runtime_role import provision


@pytest.fixture(scope="module")
def runtime_login():
    url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {"riesgo_legal", "riesgo_legal_test"}:
        pytest.fail("a dedicated Security/runtime test database is required")
    owner = dict(host=parsed.host, port=parsed.port or 5432, dbname=parsed.database,
                 user=parsed.username, password=parsed.password)
    login = f"rl_test_{uuid4().hex}"
    password = secrets.token_urlsafe(32)
    with psycopg.connect(**owner, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            assert cursor.fetchone() == ("0015_runtime_privs",)
        try:
            provision(connection, login=login, password=password)
            yield dict(host=parsed.host, port=parsed.port or 5432, dbname=parsed.database,
                       user=login, password=password)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))


@pytest.mark.requires_db
def test_exact_effective_runtime_privilege_matrix(runtime_login) -> None:
    with psycopg.connect(**runtime_login) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, session_user")
            assert cursor.fetchone() == (runtime_login["user"], runtime_login["user"])
            for table, expected in REQUIRED_PRIVILEGES.items():
                for letter, privilege in (("S", "SELECT"), ("I", "INSERT"), ("U", "UPDATE"), ("D", "DELETE")):
                    cursor.execute("SELECT has_table_privilege(current_user, %s, %s)", (table, privilege))
                    assert cursor.fetchone() == (letter in expected,), (table, privilege)
        connection.rollback()


@pytest.mark.requires_db
def test_runtime_cannot_read_or_mutate_audit_log(runtime_login) -> None:
    with psycopg.connect(**runtime_login) as connection:
        for table in ("audit.event", "audit.event_resource"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT * FROM {table} LIMIT 1")
            connection.rollback()


@pytest.mark.requires_db
def test_runtime_prestart_accepts_exact_revision_and_rejects_identity_mismatch(runtime_login) -> None:
    with psycopg.connect(**runtime_login) as connection:
        check_runtime_connection(connection, expected_user=runtime_login["user"])
        with pytest.raises(RuntimeCheckError):
            check_runtime_connection(connection, expected_user="wrong_runtime_login")


@pytest.mark.requires_db
def test_runtime_alembic_version_is_select_only(runtime_login) -> None:
    with psycopg.connect(**runtime_login) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM public.alembic_version")
            assert cursor.fetchone() == ("0015_runtime_privs",)
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                cursor.execute("SELECT has_table_privilege(current_user, 'public.alembic_version', %s)", (privilege,))
                assert cursor.fetchone() == (False,)
        connection.rollback()


@pytest.fixture(scope="module")
def guard_database():
    source_url = os.getenv("DATA_TEST_DATABASE_URL", "")
    if not source_url:
        pytest.skip("dedicated DATA_TEST_DATABASE_URL is required")
    parsed = make_url(source_url)
    if "test" not in (parsed.database or "").lower() or parsed.database in {"riesgo_legal", "riesgo_legal_test"}:
        pytest.fail("a dedicated test administrator database is required")
    name = f"sec_runtime_guard_test_{uuid4().hex}"
    admin = sa.create_engine(source_url, isolation_level="AUTOCOMMIT")
    test_url = parsed.set(database=name).render_as_string(hide_password=False)
    login = f"rl_test_{uuid4().hex}"
    password = secrets.token_urlsafe(32)
    previous = {key: os.environ.get(key) for key in
                ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")}
    owner = dict(host=parsed.host, port=parsed.port or 5432, dbname=name,
                 user=parsed.username, password=parsed.password)
    runtime = dict(host=parsed.host, port=parsed.port or 5432, dbname=name,
                   user=login, password=password)
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        os.environ.update(POSTGRES_HOST=parsed.host or "localhost", POSTGRES_PORT=str(parsed.port or 5432),
                          POSTGRES_USER=parsed.username or "", POSTGRES_PASSWORD=parsed.password or "",
                          POSTGRES_DB=name)
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        command.upgrade(config, "0015_runtime_privs")
        with psycopg.connect(**owner, autocommit=True) as connection:
            provision(connection, login=login, password=password)
        yield owner, runtime
    finally:
        with psycopg.connect(**owner, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _assert_guard_rejects(runtime: dict[str, object]) -> None:
    with psycopg.connect(**runtime) as connection:
        with pytest.raises(RuntimeCheckError):
            check_runtime_connection(connection, expected_user=str(runtime["user"]))


def _assert_guard_rejects_role_attribute(
    guard_database,
    *,
    unsafe_attribute: str,
    safe_attribute: str,
) -> None:
    owner, runtime = guard_database
    with psycopg.connect(**owner, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER ROLE {} {}").format(
                    sql.Identifier(str(runtime["user"])),
                    sql.SQL(unsafe_attribute),
                )
            )
        try:
            _assert_guard_rejects(runtime)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER ROLE {} {}").format(
                        sql.Identifier(str(runtime["user"])),
                        sql.SQL(safe_attribute),
                    )
                )


@pytest.mark.requires_db
def test_runtime_check_rejects_0014_revision(guard_database) -> None:
    owner, runtime = guard_database
    with psycopg.connect(**owner, autocommit=True) as connection:
        try:
            connection.execute("UPDATE public.alembic_version SET version_num='0014_rag_index_certificate'")
            _assert_guard_rejects(runtime)
        finally:
            connection.execute("UPDATE public.alembic_version SET version_num='0015_runtime_privs'")


@pytest.mark.requires_db
def test_runtime_check_rejects_missing_or_multiple_revision_rows(guard_database) -> None:
    owner, runtime = guard_database
    with psycopg.connect(**owner, autocommit=True) as connection:
        try:
            connection.execute("DELETE FROM public.alembic_version")
            _assert_guard_rejects(runtime)
        finally:
            connection.execute("INSERT INTO public.alembic_version (version_num) VALUES ('0015_runtime_privs')")
        try:
            connection.execute("INSERT INTO public.alembic_version (version_num) VALUES ('0014_rag_index_certificate')")
            _assert_guard_rejects(runtime)
        finally:
            connection.execute("DELETE FROM public.alembic_version WHERE version_num='0014_rag_index_certificate'")


@pytest.mark.requires_db
def test_runtime_check_fails_closed_without_version_privilege(guard_database) -> None:
    owner, runtime = guard_database
    with psycopg.connect(**owner, autocommit=True) as connection:
        try:
            connection.execute("REVOKE SELECT ON public.alembic_version FROM riesgo_legal_runtime")
            _assert_guard_rejects(runtime)
        finally:
            connection.execute("GRANT SELECT ON public.alembic_version TO riesgo_legal_runtime")


@pytest.mark.requires_db
def test_runtime_check_rejects_superuser_effective_login(guard_database) -> None:
    _assert_guard_rejects_role_attribute(
        guard_database,
        unsafe_attribute="SUPERUSER",
        safe_attribute="NOSUPERUSER",
    )


@pytest.mark.requires_db
def test_runtime_check_rejects_createdb_runtime_login(guard_database) -> None:
    _assert_guard_rejects_role_attribute(
        guard_database,
        unsafe_attribute="CREATEDB",
        safe_attribute="NOCREATEDB",
    )


@pytest.mark.requires_db
def test_runtime_check_rejects_createrole_runtime_login(guard_database) -> None:
    _assert_guard_rejects_role_attribute(
        guard_database,
        unsafe_attribute="CREATEROLE",
        safe_attribute="NOCREATEROLE",
    )


@pytest.mark.requires_db
def test_runtime_check_rejects_bypassrls_runtime_login(guard_database) -> None:
    _assert_guard_rejects_role_attribute(
        guard_database,
        unsafe_attribute="BYPASSRLS",
        safe_attribute="NOBYPASSRLS",
    )


@pytest.mark.requires_db
def test_runtime_check_rejects_missing_runtime_group_membership(guard_database) -> None:
    owner, runtime = guard_database
    with psycopg.connect(**owner, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("REVOKE riesgo_legal_runtime FROM {}").format(
                    sql.Identifier(str(runtime["user"]))
                )
            )
        try:
            _assert_guard_rejects(runtime)
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("GRANT riesgo_legal_runtime TO {}").format(
                        sql.Identifier(str(runtime["user"]))
                    )
                )
