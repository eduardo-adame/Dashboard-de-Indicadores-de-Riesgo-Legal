"""Aprovisionamiento del login limitado por el operador; los grants pertenecen a Alembic."""
from __future__ import annotations

import os

import psycopg
from psycopg import sql


RUNTIME_GROUP = "riesgo_legal_runtime"


class ProvisioningError(RuntimeError):
    """No se puede establecer un login runtime seguro."""


def provision(connection: psycopg.Connection, *, login: str, password: str) -> None:
    if not login or login == RUNTIME_GROUP or not password:
        raise ProvisioningError("Runtime login and protected password are required")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (RUNTIME_GROUP,))
            group = cursor.fetchone()
            if group != (False,):
                raise ProvisioningError("Runtime group role is unavailable")
            cursor.execute(
                "SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (login,),
            )
            existing = cursor.fetchone()
            expected = (True, True, False, False, False, False)
            if existing is not None and existing != expected:
                raise ProvisioningError("Existing runtime login has unexpected attributes")
            if existing is None:
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOBYPASSRLS PASSWORD {}"
                    ).format(sql.Identifier(login), sql.Literal(password))
                )
            else:
                cursor.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(login), sql.Literal(password)
                    )
                )
            cursor.execute(
                sql.SQL("GRANT {} TO {} WITH INHERIT TRUE").format(
                    sql.Identifier(RUNTIME_GROUP), sql.Identifier(login)
                )
            )
            cursor.execute(
                "SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (login,),
            )
            if cursor.fetchone() != expected:
                raise ProvisioningError("Runtime login verification failed")
            cursor.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER'), pg_has_role(%s, %s, 'USAGE')",
                (login, RUNTIME_GROUP, login, RUNTIME_GROUP),
            )
            if cursor.fetchone() != (True, True):
                raise ProvisioningError("Runtime membership verification failed")


def main() -> None:
    login = os.getenv("RUNTIME_POSTGRES_USER", "")
    password = os.getenv("RUNTIME_POSTGRES_PASSWORD", "")
    owner_user = os.getenv("POSTGRES_USER", "")
    owner_password = os.getenv("POSTGRES_PASSWORD", "")
    if not owner_user or not owner_password or login == owner_user:
        raise ProvisioningError("Migration and runtime identities must be separate")
    try:
        with psycopg.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            dbname=os.getenv("POSTGRES_DB", "riesgo_legal"),
            user=owner_user,
            password=owner_password,
        ) as connection:
            provision(connection, login=login, password=password)
    except psycopg.Error as exc:
        raise ProvisioningError("Runtime login provisioning failed") from exc


if __name__ == "__main__":
    try:
        main()
    except ProvisioningError as exc:
        raise SystemExit(str(exc)) from None
