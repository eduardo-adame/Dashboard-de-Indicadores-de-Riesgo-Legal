"""Acceso transaccional a cuentas, sesiones, ACL y auditoría."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import re
from typing import Iterator
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from app.security.models import AuditPersistenceError, AuthenticatedPrincipal, KNOWN_FAMILIES, KNOWN_ROLES


class SecurityRepository:
    """Persistencia PostgreSQL sin ORM para operaciones de seguridad."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        try:
            with psycopg.connect(self._conninfo, row_factory=dict_row) as connection:
                with connection.transaction():
                    yield connection
        except psycopg.Error as exc:
            raise AuditPersistenceError("La operación no pudo confirmarse") from exc

    @staticmethod
    def account_by_username(connection: psycopg.Connection, username: str) -> dict[str, object] | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, username, display_name, password_hash, state, authorization_version
                   FROM app.user_account WHERE username = %s FOR UPDATE""",
                (username,),
            )
            return cursor.fetchone()

    @staticmethod
    def account_by_id(connection: psycopg.Connection, account_id: UUID, *, lock: bool = False) -> dict[str, object] | None:
        suffix = " FOR UPDATE" if lock else ""
        with connection.cursor() as cursor:
            cursor.execute(
                f"""SELECT id, username, display_name, password_hash, state, authorization_version
                    FROM app.user_account WHERE id = %s{suffix}""",
                (account_id,),
            )
            return cursor.fetchone()

    @staticmethod
    def _roles_and_permissions(connection: psycopg.Connection, account_id: UUID) -> tuple[frozenset[str], frozenset[str]]:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT r.id AS role_id, p.id AS permission_id
                   FROM app.user_role ur
                   JOIN app.role r ON r.id = ur.role_id AND r.active
                   JOIN app.role_permission rp ON rp.role_id = r.id
                   JOIN app.permission p ON p.id = rp.permission_id
                   WHERE ur.user_id = %s AND ur.active""",
                (account_id,),
            )
            rows = cursor.fetchall()
        return (
            frozenset(str(row["role_id"]) for row in rows),
            frozenset(str(row["permission_id"]) for row in rows),
        )

    def principal_for_session(self, connection: psycopg.Connection, account_id: UUID, session_id: UUID, authorization_version: int) -> AuthenticatedPrincipal | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT u.id, u.username, u.state, u.authorization_version,
                          s.state AS session_state, s.expires_at,
                          s.authorization_version AS session_authorization_version
                   FROM app.user_account u
                   JOIN app.access_session s ON s.user_id = u.id
                   WHERE u.id = %s AND s.id = %s FOR UPDATE""",
                (account_id, session_id),
            )
            row = cursor.fetchone()
        if row is None or row["state"] != "ACTIVE" or row["session_state"] != "ACTIVE":
            return None
        expires_at = row["expires_at"]
        if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
            return None
        current_version = int(row["authorization_version"])
        if int(row["session_authorization_version"]) != current_version or authorization_version != current_version:
            return None
        roles, permissions = self._roles_and_permissions(connection, account_id)
        return AuthenticatedPrincipal(
            account_id=account_id,
            session_id=session_id,
            username=str(row["username"]),
            authorization_version=current_version,
            roles=roles,
            permissions=permissions,
        )

    @staticmethod
    def create_session(connection: psycopg.Connection, *, account_id: UUID, authorization_version: int, token_hash: bytes, expires_at: datetime, correlation_id: UUID) -> UUID:
        session_id = uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app.access_session
                   (id, user_id, refresh_token_sha256, authorization_version, state, expires_at, correlation_id)
                   VALUES (%s, %s, %s, %s, 'ACTIVE', %s, %s)""",
                (session_id, account_id, token_hash, authorization_version, expires_at, correlation_id),
            )
        return session_id

    @staticmethod
    def refresh_session(connection: psycopg.Connection, *, token_hash: bytes, replacement_hash: bytes) -> dict[str, object] | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT s.id, s.user_id, s.authorization_version, s.expires_at,
                          s.state AS session_state, u.state AS account_state,
                          u.authorization_version AS account_authorization_version
                   FROM app.access_session s
                   JOIN app.user_account u ON u.id = s.user_id
                   WHERE s.refresh_token_sha256 = %s FOR UPDATE""",
                (token_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            expires_at = row["expires_at"]
            if (
                row["session_state"] != "ACTIVE"
                or row["account_state"] != "ACTIVE"
                or not isinstance(expires_at, datetime)
                or expires_at <= datetime.now(UTC)
                or int(row["authorization_version"]) != int(row["account_authorization_version"])
            ):
                return None
            cursor.execute(
                "UPDATE app.access_session SET refresh_token_sha256 = %s WHERE id = %s",
                (replacement_hash, row["id"]),
            )
            return row

    @staticmethod
    def invalidate_sessions(connection: psycopg.Connection, account_ids: list[UUID]) -> list[UUID]:
        if not account_ids:
            return []
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM app.access_session WHERE user_id = ANY(%s) AND state = 'ACTIVE' FOR UPDATE",
                (account_ids,),
            )
            session_ids = [row["id"] for row in cursor.fetchall()]
            cursor.execute(
                "UPDATE app.user_account SET authorization_version = authorization_version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ANY(%s)",
                (account_ids,),
            )
            cursor.execute(
                """UPDATE app.access_session SET state = 'INVALIDATED', invalidated_at = CURRENT_TIMESTAMP
                   WHERE user_id = ANY(%s) AND state = 'ACTIVE'""",
                (account_ids,),
            )
        return session_ids

    @staticmethod
    def invalidate_one_session(connection: psycopg.Connection, session_id: UUID) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE app.access_session SET state = 'LOGGED_OUT', invalidated_at = CURRENT_TIMESTAMP
                   WHERE id = %s AND state = 'ACTIVE'""",
                (session_id,),
            )

    @staticmethod
    def active_accounts_for_role(connection: psycopg.Connection, role_id: str) -> list[UUID]:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT ur.user_id FROM app.user_role ur
                   JOIN app.user_account u ON u.id = ur.user_id
                   WHERE ur.role_id = %s AND ur.active AND u.state = 'ACTIVE'""",
                (role_id,),
            )
            return [row["user_id"] for row in cursor.fetchall()]

    @staticmethod
    def acquire_initial_admin_lock(connection: psycopg.Connection) -> None:
        """Serializa el aprovisionamiento de la primera identidad administrativa."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('security.initial_admin_bootstrap'))")

    @staticmethod
    def has_active_ti_account(connection: psycopg.Connection) -> bool:
        """Indica si ya existe una identidad TI capaz de administrar normalmente."""
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT EXISTS(
                       SELECT 1
                       FROM app.user_role ur
                       JOIN app.user_account u ON u.id = ur.user_id
                       WHERE ur.role_id = 'TI' AND ur.active AND u.state = 'ACTIVE'
                   )"""
            )
            return bool(cursor.fetchone()["exists"])

    @staticmethod
    def create_account(connection: psycopg.Connection, *, username: str, display_name: str, password_hash: str) -> UUID:
        account_id = uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app.user_account (id, username, display_name, password_hash)
                   VALUES (%s, %s, %s, %s)""",
                (account_id, username, display_name, password_hash),
            )
        return account_id

    @staticmethod
    def update_account(connection: psycopg.Connection, account_id: UUID, display_name: str) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.user_account SET display_name = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND state = 'ACTIVE'",
                (display_name, account_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def set_account_state(connection: psycopg.Connection, account_id: UUID, state: str) -> bool:
        with connection.cursor() as cursor:
            if state == "DISABLED":
                cursor.execute(
                    """UPDATE app.user_account SET state = 'DISABLED', disabled_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP WHERE id = %s AND state = 'ACTIVE'""",
                    (account_id,),
                )
            else:
                cursor.execute(
                    """UPDATE app.user_account SET state = 'ACTIVE', disabled_at = NULL,
                       updated_at = CURRENT_TIMESTAMP WHERE id = %s AND state = 'DISABLED'""",
                    (account_id,),
                )
            return cursor.rowcount == 1

    @staticmethod
    def set_password(connection: psycopg.Connection, account_id: UUID, password_hash: str) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE app.user_account SET password_hash = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND state = 'ACTIVE'",
                (password_hash, account_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def set_roles(connection: psycopg.Connection, account_id: UUID, roles: frozenset[str]) -> None:
        if not roles.issubset(KNOWN_ROLES):
            raise ValueError("Rol desconocido")
        with connection.cursor() as cursor:
            cursor.execute(
                """UPDATE app.user_role SET active = false, revoked_at = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND active""",
                (account_id,),
            )
            for role_id in roles:
                cursor.execute(
                    """INSERT INTO app.user_role (user_id, role_id, active, revoked_at)
                       VALUES (%s, %s, true, NULL)
                       ON CONFLICT (user_id, role_id) DO UPDATE
                       SET active = true, revoked_at = NULL, assigned_at = CURRENT_TIMESTAMP""",
                    (account_id, role_id),
                )

    @staticmethod
    def has_active_ti_role(connection: psycopg.Connection, account_id: UUID) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM app.user_role WHERE user_id = %s AND role_id = 'TI' AND active)",
                (account_id,),
            )
            return bool(cursor.fetchone()["exists"])

    @staticmethod
    def is_last_active_ti(connection: psycopg.Connection, account_id: UUID) -> bool:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT count(*) AS total FROM app.user_role ur
                   JOIN app.user_account u ON u.id = ur.user_id
                   WHERE ur.role_id = 'TI' AND ur.active AND u.state = 'ACTIVE'"""
            )
            return int(cursor.fetchone()["total"]) == 1 and SecurityRepository.has_active_ti_role(connection, account_id)

    @staticmethod
    def set_scope_grant(connection: psycopg.Connection, role_id: str, source_family: str, active: bool) -> None:
        if role_id not in KNOWN_ROLES or source_family not in KNOWN_FAMILIES:
            raise ValueError("Ámbito documental desconocido")
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO app.document_scope_grant (id, role_id, source_family, active)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (role_id, source_family) DO UPDATE SET active = EXCLUDED.active""",
                (uuid4(), role_id, source_family, active),
            )

    @staticmethod
    def set_document_exception(connection: psycopg.Connection, *, account_id: UUID | None, role_id: str | None, document_id: str, decision: str, active: bool) -> None:
        if decision not in {"ALLOW", "DENY"}:
            raise ValueError("Decisión documental desconocida")
        with connection.cursor() as cursor:
            if account_id is not None:
                cursor.execute(
                    """UPDATE app.document_exception SET decision = %s, active = %s,
                       revoked_at = CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP END
                       WHERE active AND user_id = %s AND id_documento = %s""",
                    (decision, active, active, account_id, document_id),
                )
                if active and cursor.rowcount == 0:
                    cursor.execute("""INSERT INTO app.document_exception
                        (id, user_id, id_documento, decision, active) VALUES (%s, %s, %s, %s, true)""",
                        (uuid4(), account_id, document_id, decision))
            elif role_id is not None:
                cursor.execute(
                    """UPDATE app.document_exception SET decision = %s, active = %s,
                       revoked_at = CASE WHEN %s THEN NULL ELSE CURRENT_TIMESTAMP END
                       WHERE active AND role_id = %s AND id_documento = %s""",
                    (decision, active, active, role_id, document_id),
                )
                if active and cursor.rowcount == 0:
                    cursor.execute("""INSERT INTO app.document_exception
                        (id, role_id, id_documento, decision, active) VALUES (%s, %s, %s, %s, true)""",
                        (uuid4(), role_id, document_id, decision))
            else:
                raise ValueError("La excepción requiere una cuenta o un rol")

    @staticmethod
    def document_rules(connection: psycopg.Connection, account_id: UUID, roles: frozenset[str], document_id: str) -> tuple[set[str], set[str], set[str]]:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT decision FROM app.document_exception
                   WHERE active AND id_documento = %s
                   AND (user_id = %s OR role_id = ANY(%s))""",
                (document_id, account_id, list(roles)),
            )
            decisions = {str(row["decision"]) for row in cursor.fetchall()}
            cursor.execute(
                """SELECT source_family FROM app.document_scope_grant
                   WHERE active AND role_id = ANY(%s)""",
                (list(roles),),
            )
            families = {str(row["source_family"]) for row in cursor.fetchall()}
        return decisions, families, set()

    @staticmethod
    def write_audit_event(connection: psycopg.Connection, *, actor: AuthenticatedPrincipal | None, action: str, resource_type: str, resource_identifier: str | None, result: str, correlation_id: UUID, safe_cause_code: str | None = None, process_identifier: str | None = None) -> None:
        if actor is not None:
            actor_type, actor_identifier, actor_user_id = "HUMAN", actor.username, actor.account_id
        elif process_identifier:
            actor_type, actor_identifier, actor_user_id = "PROCESS", process_identifier, None
        else:
            actor_type, actor_identifier, actor_user_id = "ANONYMOUS", "anonymous", None
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO audit.event
                   (id, actor_type, actor_identifier, actor_user_id, action, resource_type,
                    resource_identifier, result, safe_cause_code, operation_id, correlation_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    uuid4(), actor_type, actor_identifier, actor_user_id,
                    action, resource_type, resource_identifier, result, safe_cause_code,
                    uuid4(), correlation_id,
                ),
            )

    @staticmethod
    def write_rag_audit_event(
        connection: psycopg.Connection,
        *,
        actor: AuthenticatedPrincipal,
        rag_operation_id: UUID,
        operation_id: UUID,
        correlation_id: UUID,
        query_sha256: bytes,
        action: str,
        result: str,
        safe_cause_code: str | None,
        resources: tuple[tuple[str, UUID], ...],
    ) -> UUID:
        """Añade un evento RAG final usando la transacción abierta del caller.

        La aplicación RAG controla la serialización de la operación y la resolución
        de reintentos. Un ID de evento en conflicto siempre es un error aquí; nunca
        se presupone que sea un reintento.
        """
        if action not in {"RAG_QUERY", "AUTHORIZATION_DENIED"}:
            raise AuditPersistenceError("Unsupported RAG audit action")
        if not isinstance(query_sha256, bytes) or len(query_sha256) != 32:
            raise AuditPersistenceError("Invalid RAG query hash")
        if not result or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", result):
            raise AuditPersistenceError("Invalid RAG audit result")
        if safe_cause_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", safe_cause_code):
            raise AuditPersistenceError("Invalid safe cause code")
        if action == "AUTHORIZATION_DENIED" and (result != "DENIED" or resources):
            raise AuditPersistenceError("Authorization denial cannot include resources")
        if action == "RAG_QUERY" and result == "DENIED":
            raise AuditPersistenceError("RAG action and result mismatch")
        if len(set(resources)) != len(resources):
            raise AuditPersistenceError("Duplicate RAG audit resource")

        event_id = uuid5(NAMESPACE_URL, f"riesgo-legal:RAG_QUERY:{str(operation_id).lower()}")
        try:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT r.id, r.user_id, r.session_id, r.query_sha256, r.state, "
                    "r.operation_id, r.correlation_id, u.username "
                    "FROM app.rag_operation r "
                    "JOIN app.user_account u ON u.id = r.user_id WHERE r.id = %s",
                    (rag_operation_id,),
                )
                operation = cursor.fetchone()
                if (
                    operation is None
                    or operation["id"] != rag_operation_id
                    or operation["operation_id"] != operation_id
                    or operation["correlation_id"] != correlation_id
                    or operation["user_id"] != actor.account_id
                    or operation["username"] != actor.username
                    or (operation["session_id"] is not None and operation["session_id"] != actor.session_id)
                    or bytes(operation["query_sha256"]) != query_sha256
                ):
                    raise AuditPersistenceError("RAG operation identity mismatch")
                if action == "AUTHORIZATION_DENIED":
                    if operation["state"] != "SIN_AUTORIZACION":
                        raise AuditPersistenceError("RAG denial state mismatch")
                elif operation["state"] == "SIN_AUTORIZACION":
                    raise AuditPersistenceError("RAG state and action mismatch")

                cursor.execute(
                    "SELECT f.fragment_id, v.id_documento "
                    "FROM app.rag_final_fragment f "
                    "JOIN app.document_chunk c ON c.id = f.fragment_id "
                    "JOIN app.document_version v ON v.id = c.document_version_id "
                    "WHERE f.rag_operation_id = %s",
                    (rag_operation_id,),
                )
                persisted = {(str(row["id_documento"]), row["fragment_id"]) for row in cursor.fetchall()}
                if set(resources) != persisted:
                    raise AuditPersistenceError("RAG final resource provenance mismatch")

                cursor.execute(
                    "INSERT INTO audit.event "
                    "(id, actor_type, actor_identifier, actor_user_id, action, resource_type, "
                    "resource_identifier, result, safe_cause_code, operation_id, correlation_id, query_sha256) "
                    "VALUES (%s, 'HUMAN', %s, %s, %s, 'RAG_OPERATION', %s, %s, %s, %s, %s, %s)",
                    (
                        event_id, actor.username, actor.account_id, action, str(rag_operation_id),
                        result, safe_cause_code, operation_id, correlation_id, query_sha256,
                    ),
                )
                for document_id, fragment_id in resources:
                    cursor.execute(
                        "INSERT INTO audit.event_resource "
                        "(event_id, resource_type, resource_identifier, id_documento, fragment_id) "
                        "VALUES (%s, 'DOCUMENT_FRAGMENT', %s, %s, %s)",
                        (event_id, str(fragment_id), document_id, fragment_id),
                    )
        except psycopg.Error as exc:
            raise AuditPersistenceError("RAG audit persistence failed") from exc
        return event_id
