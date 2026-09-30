"""Verificación previa de solo lectura y cierre seguro del login limitado de BD."""
from __future__ import annotations

import os

import psycopg

from app.config import get_settings


EXPECTED_REVISION = "0015_runtime_privs"
RUNTIME_GROUP = "riesgo_legal_runtime"

# Contrato DML efectivo exacto, incluidas las tablas añadidas después de Security.
REQUIRED_PRIVILEGES: dict[str, str] = {
    "app.coordination_dispatch": "SIU",
    "app.document_candidate": "SIU",
    "app.document_candidate_page": "SID",
    "app.proactive_input_snapshot": "SI",
    "app.proactive_evaluation": "SI",
    "app.document_index_certificate": "SI",
    "app.document_chunk_index_receipt": "SI",
    "app.rag_operation": "SI",
    "app.rag_final_fragment": "SI",
    "app.document": "SIU",
    "app.document_version": "SIU",
    "app.document_chunk": "SIU",
    "app.context_reference": "SI",
    "app.user_account": "SIU",
    "app.access_session": "SIU",
    "app.user_role": "SIU",
    "app.role": "S",
    "app.permission": "S",
    "app.role_permission": "S",
    "app.document_scope_grant": "SIU",
    "app.document_exception": "SIU",
    "audit.event": "I",
    "audit.event_resource": "I",
    "public.alembic_version": "S",
}

_DML = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE"}
_EXTRA = ("TRUNCATE", "REFERENCES", "TRIGGER")


class RuntimeCheckError(RuntimeError):
    """El backend no debe iniciar con esta identidad o esquema de base de datos."""


def check_runtime_connection(connection: psycopg.Connection, *, expected_user: str) -> None:
    """Inspecciona identidad, revisión exacta y privilegios efectivos sin escrituras."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute("SELECT current_user, session_user")
            current_user, session_user = cursor.fetchone()
            if not expected_user or current_user != expected_user or session_user != expected_user:
                raise RuntimeCheckError("Unexpected runtime database identity")
            cursor.execute(
                "SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (expected_user,),
            )
            role = cursor.fetchone()
            if role != (True, True, False, False, False, False):
                raise RuntimeCheckError("Unsafe runtime role attributes")
            cursor.execute(
                "SELECT pg_has_role(%s, %s, 'MEMBER'), pg_has_role(%s, %s, 'USAGE')",
                (expected_user, RUNTIME_GROUP, expected_user, RUNTIME_GROUP),
            )
            if cursor.fetchone() != (True, True):
                raise RuntimeCheckError("Runtime group membership is not inherited")
            cursor.execute("SELECT version_num FROM public.alembic_version")
            versions = cursor.fetchall()
            if len(versions) != 1 or versions[0][0] != EXPECTED_REVISION:
                raise RuntimeCheckError("Unexpected Alembic revision")
            for table, required in REQUIRED_PRIVILEGES.items():
                for letter, privilege in _DML.items():
                    cursor.execute("SELECT has_table_privilege(%s, %s, %s)", (expected_user, table, privilege))
                    if bool(cursor.fetchone()[0]) != (letter in required):
                        raise RuntimeCheckError("Runtime table privileges do not match contract")
                for privilege in _EXTRA:
                    cursor.execute("SELECT has_table_privilege(%s, %s, %s)", (expected_user, table, privilege))
                    if bool(cursor.fetchone()[0]):
                        raise RuntimeCheckError("Unexpected runtime table privilege")
    except psycopg.Error as exc:
        raise RuntimeCheckError("Runtime database contract cannot be verified") from exc
    finally:
        connection.rollback()


def check_runtime() -> None:
    """Conecta con las credenciales efectivas del backend y aplica cierre seguro."""
    settings = get_settings()
    expected_user = os.getenv("RUNTIME_POSTGRES_USER", "")
    if not expected_user or settings.postgres_user != expected_user:
        raise RuntimeCheckError("Runtime login is not configured")
    try:
        with psycopg.connect(settings.psycopg_conninfo) as connection:
            check_runtime_connection(connection, expected_user=expected_user)
    except psycopg.Error as exc:
        raise RuntimeCheckError("Runtime database connection unavailable") from exc


if __name__ == "__main__":
    try:
        check_runtime()
    except RuntimeCheckError as exc:
        raise SystemExit(str(exc)) from None
