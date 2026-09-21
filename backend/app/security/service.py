"""Casos de uso de autenticación, RBAC y ACL documental."""
from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.security.models import (
    AuthenticatedPrincipal,
    AuthenticationError,
    BootstrapAlreadyCompletedError,
    AuthorizationContext,
    AuthorizationError,
    AuthorizedDocumentScope,
    DocumentAuthorizationDecision,
    DocumentAuthorizationResult,
    FunctionalAuthorizationDecision,
    KNOWN_FAMILIES,
    KNOWN_ROLES,
    ValidationError,
)
from app.security.passwords import PasswordService
from app.security.repository import SecurityRepository
from app.security.tokens import JwtService


class SecurityService:
    """Coordina persistencia y decisiones de seguridad vigentes."""

    def __init__(self, repository: SecurityRepository, jwt_service: JwtService | None = None, password_service: PasswordService | None = None) -> None:
        self.repository = repository
        self.jwt_service = jwt_service
        self.passwords = password_service or PasswordService()

    def _jwt(self) -> JwtService:
        """Obtiene la configuración JWT solo para operaciones que la requieren."""
        if self.jwt_service is None:
            raise AuthenticationError("Credenciales o sesión no válidas")
        return self.jwt_service

    @staticmethod
    def _refresh_token() -> str:
        """Genera un token opaco con 256 bits de aleatoriedad."""
        return secrets.token_urlsafe(32)

    @staticmethod
    def _token_hash(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    @staticmethod
    def _require(principal: AuthenticatedPrincipal, capability: str) -> FunctionalAuthorizationDecision:
        permitted = capability in principal.permissions
        decision = FunctionalAuthorizationDecision(
            permitted=permitted,
            capability=capability,
            authorization_version=principal.authorization_version,
            safe_reason="PERMITTED" if permitted else "DEFAULT_DENY",
        )
        if not permitted:
            raise AuthorizationError("Acceso no autorizado")
        return decision

    def _authoritative_principal(self, connection, actor: AuthenticatedPrincipal, capability: str | None = None) -> AuthenticatedPrincipal:
        """Revalida cuenta, sesión, versión y roles bajo el bloqueo de la transacción."""
        current = self.repository.principal_for_session(
            connection, actor.account_id, actor.session_id, actor.authorization_version
        )
        if current is None:
            raise AuthenticationError("Credenciales o sesión no válidas")
        if capability is not None:
            self._require(current, capability)
        return current

    def revalidate_functional_access(
        self,
        connection,
        principal: AuthenticatedPrincipal,
        capability: str,
    ) -> AuthenticatedPrincipal:
        """Revalida acceso funcional usando la transacción abierta del consumidor.

        La persona consumidora conserva la frontera de transacción y debe registrar
        mediante Audit cualquier denegación aplicable antes de devolver el error
        seguro. Esta operación no abre, confirma ni revierte una transacción.
        """
        return self._authoritative_principal(connection, principal, capability)

    def _invalidate_accounts(self, connection, actor: AuthenticatedPrincipal, account_ids: list[UUID], correlation: UUID) -> None:
        """Invalida sesiones y deja un evento minimizado por cada sesión afectada."""
        for session_id in self.repository.invalidate_sessions(connection, account_ids):
            self.repository.write_audit_event(
                connection, actor=actor, action="SESSION_INVALIDATION", resource_type="SESSION",
                resource_identifier=str(session_id), result="SUCCESS", correlation_id=correlation,
            )

    @staticmethod
    def _validate_account_input(username: str, display_name: str, roles: frozenset[str]) -> tuple[str, str]:
        normalized_username = username.strip()
        normalized_display_name = display_name.strip()
        if not normalized_username or not normalized_display_name or not roles or not roles.issubset(KNOWN_ROLES):
            raise ValidationError("Datos de cuenta no válidos")
        return normalized_username, normalized_display_name

    def bootstrap_first_ti(
        self,
        *,
        username: str,
        password: str,
        display_name: str | None = None,
        correlation_id: UUID | None = None,
    ) -> UUID:
        """Crea una única identidad TI inicial mediante una acción local explícita.

        No sustituye la administración normal: tras crear una TI activa, cualquier
        nueva invocación falla antes de modificar cuentas, roles o contraseñas.
        """
        normalized_username, normalized_display_name = self._validate_account_input(
            username, display_name or username, frozenset({"TI"})
        )
        password_hash = self.passwords.hash(password)
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            self.repository.acquire_initial_admin_lock(connection)
            if self.repository.has_active_ti_account(connection):
                raise BootstrapAlreadyCompletedError("BOOTSTRAP_ALREADY_COMPLETED")
            account_id = self.repository.create_account(
                connection,
                username=normalized_username,
                display_name=normalized_display_name,
                password_hash=password_hash,
            )
            self.repository.set_roles(connection, account_id, frozenset({"TI"}))
            self.repository.write_audit_event(
                connection,
                actor=None,
                action="SECURITY_BOOTSTRAP",
                resource_type="USER",
                resource_identifier=str(account_id),
                result="SUCCESS",
                correlation_id=correlation,
                safe_cause_code="INITIAL_ADMINISTRATION",
                process_identifier="security-bootstrap",
            )
            return account_id

    def login(self, username: str, password: str, correlation_id: UUID | None = None) -> tuple[str, str]:
        """Crea una sesión absoluta y devuelve access JWT más refresh opaco."""
        correlation = correlation_id or uuid4()
        denied = False
        result: tuple[str, str] | None = None
        with self.repository.transaction() as connection:
            account = self.repository.account_by_username(connection, username)
            valid = self.passwords.verify(password, str(account["password_hash"]) if account else None)
            if not account or not valid or account["state"] != "ACTIVE":
                self.repository.write_audit_event(
                    connection, actor=None, action="AUTH_LOGIN", resource_type="SESSION",
                    resource_identifier=None, result="DENIED", correlation_id=correlation,
                    safe_cause_code="INVALID_CREDENTIALS",
                )
                denied = True
            else:
                account_id = account["id"]
                authorization_version = int(account["authorization_version"])
                refresh_token = self._refresh_token()
                session_id = self.repository.create_session(
                    connection, account_id=account_id, authorization_version=authorization_version,
                    token_hash=self._token_hash(refresh_token), expires_at=datetime.now(UTC) + timedelta(hours=8),
                    correlation_id=correlation,
                )
                roles, permissions = self.repository._roles_and_permissions(connection, account_id)
                principal = AuthenticatedPrincipal(account_id, session_id, str(account["username"]), authorization_version, roles, permissions)
                self.repository.write_audit_event(connection, actor=principal, action="AUTH_LOGIN", resource_type="SESSION", resource_identifier=str(session_id), result="SUCCESS", correlation_id=correlation)
                result = (self._jwt().issue(account_id=account_id, session_id=session_id, authorization_version=authorization_version), refresh_token)
        if denied or result is None:
            raise AuthenticationError("Credenciales o sesión no válidas")
        return result

    def refresh(self, refresh_token: str, correlation_id: UUID | None = None) -> tuple[str, str]:
        """Rota el refresh sin mover la expiración absoluta de la sesión."""
        correlation = correlation_id or uuid4()
        replacement = self._refresh_token()
        denied = False
        result: tuple[str, str] | None = None
        with self.repository.transaction() as connection:
            row = self.repository.refresh_session(
                connection, token_hash=self._token_hash(refresh_token), replacement_hash=self._token_hash(replacement)
            )
            if row is None:
                self.repository.write_audit_event(
                    connection, actor=None, action="AUTH_REFRESH", resource_type="SESSION",
                    resource_identifier=None, result="DENIED", correlation_id=correlation,
                    safe_cause_code="INVALID_SESSION",
                )
                denied = True
            else:
                principal = self.repository.principal_for_session(connection, row["user_id"], row["id"], int(row["account_authorization_version"]))
                if principal is None:
                    self.repository.write_audit_event(
                        connection, actor=None, action="AUTH_REFRESH", resource_type="SESSION",
                        resource_identifier=str(row["id"]), result="DENIED", correlation_id=correlation,
                        safe_cause_code="INVALID_SESSION",
                    )
                    denied = True
                else:
                    self.repository.write_audit_event(connection, actor=principal, action="AUTH_REFRESH", resource_type="SESSION", resource_identifier=str(principal.session_id), result="SUCCESS", correlation_id=correlation)
                    result = (self._jwt().issue(account_id=principal.account_id, session_id=principal.session_id, authorization_version=principal.authorization_version), replacement)
        if denied or result is None:
            raise AuthenticationError("Credenciales o sesión no válidas")
        return result

    def authenticated_principal(self, access_token: str) -> AuthenticatedPrincipal:
        """Valida JWT y estado persistido en cada petición protegida."""
        try:
            claims = self._jwt().decode(access_token)
        except AuthenticationError:
            with self.repository.transaction() as connection:
                self.repository.write_audit_event(
                    connection, actor=None, action="AUTH_TOKEN_VALIDATION", resource_type="TOKEN",
                    resource_identifier=None, result="DENIED", correlation_id=uuid4(),
                    safe_cause_code="INVALID_TOKEN",
                )
            raise
        account_id = UUID(str(claims["sub"]))
        session_id = UUID(str(claims["sid"]))
        result: AuthenticatedPrincipal | None = None
        with self.repository.transaction() as connection:
            principal = self.repository.principal_for_session(connection, account_id, session_id, int(claims["av"]))
            if principal is None:
                self.repository.write_audit_event(
                    connection, actor=None, action="AUTH_SESSION_VALIDATION", resource_type="SESSION",
                    resource_identifier=str(session_id), result="DENIED", correlation_id=uuid4(),
                    safe_cause_code="INVALID_SESSION",
                )
            else:
                result = principal
        if result is None:
            raise AuthenticationError("Credenciales o sesión no válidas")
        return result

    def require_functional_permission(self, principal: AuthenticatedPrincipal, capability: str, correlation_id: UUID | None = None) -> FunctionalAuthorizationDecision:
        """Niega por defecto y registra la denegación funcional aplicable."""
        correlation = correlation_id or uuid4()
        result: FunctionalAuthorizationDecision | None = None
        with self.repository.transaction() as connection:
            current = self.repository.principal_for_session(connection, principal.account_id, principal.session_id, principal.authorization_version)
            if current is not None and capability in current.permissions:
                result = FunctionalAuthorizationDecision(True, capability, current.authorization_version, "PERMITTED")
            else:
                self.repository.write_audit_event(
                    connection, actor=principal, action="AUTHORIZATION_DENIED", resource_type="CAPABILITY",
                    resource_identifier=capability, result="DENIED", correlation_id=correlation,
                    safe_cause_code="DEFAULT_DENY",
                )
        if result is None:
            raise AuthorizationError("Acceso no autorizado")
        return result

    def logout(self, principal: AuthenticatedPrincipal, correlation_id: UUID | None = None) -> None:
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            principal = self._authoritative_principal(connection, principal)
            self.repository.invalidate_one_session(connection, principal.session_id)
            self.repository.write_audit_event(
                connection, actor=principal, action="AUTH_LOGOUT", resource_type="SESSION",
                resource_identifier=str(principal.session_id), result="SUCCESS", correlation_id=correlation,
            )

    def create_account(self, actor: AuthenticatedPrincipal, *, username: str, display_name: str, password: str, roles: frozenset[str], correlation_id: UUID | None = None) -> UUID:
        self._require(actor, "user.create")
        username, display_name = self._validate_account_input(username, display_name, roles)
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "user.create")
            account_id = self.repository.create_account(
                connection, username=username, display_name=display_name, password_hash=self.passwords.hash(password)
            )
            self.repository.set_roles(connection, account_id, roles)
            self.repository.write_audit_event(
                connection, actor=actor, action="ACCOUNT_CREATE", resource_type="USER",
                resource_identifier=str(account_id), result="SUCCESS", correlation_id=correlation,
            )
            return account_id

    def update_account(self, actor: AuthenticatedPrincipal, account_id: UUID, display_name: str, correlation_id: UUID | None = None) -> None:
        self._require(actor, "user.update")
        if not display_name.strip():
            raise ValidationError("Datos de cuenta no válidos")
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "user.update")
            if not self.repository.update_account(connection, account_id, display_name.strip()):
                raise AuthorizationError("Acceso no autorizado")
            self.repository.write_audit_event(connection, actor=actor, action="ACCOUNT_UPDATE", resource_type="USER", resource_identifier=str(account_id), result="SUCCESS", correlation_id=correlation)

    def disable_account(self, actor: AuthenticatedPrincipal, account_id: UUID, correlation_id: UUID | None = None) -> None:
        self._require(actor, "user.disable")
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "user.disable")
            if self.repository.is_last_active_ti(connection, account_id):
                raise ValidationError("La operación no está permitida")
            if not self.repository.set_account_state(connection, account_id, "DISABLED"):
                raise AuthorizationError("Acceso no autorizado")
            self._invalidate_accounts(connection, actor, [account_id], correlation)
            self.repository.write_audit_event(connection, actor=actor, action="ACCOUNT_DISABLE", resource_type="USER", resource_identifier=str(account_id), result="SUCCESS", correlation_id=correlation)

    def activate_account(self, actor: AuthenticatedPrincipal, account_id: UUID, correlation_id: UUID | None = None) -> None:
        self._require(actor, "user.update")
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "user.update")
            if not self.repository.set_account_state(connection, account_id, "ACTIVE"):
                raise AuthorizationError("Acceso no autorizado")
            self._invalidate_accounts(connection, actor, [account_id], correlation)
            self.repository.write_audit_event(connection, actor=actor, action="ACCOUNT_ACTIVATE", resource_type="USER", resource_identifier=str(account_id), result="SUCCESS", correlation_id=correlation)

    def reset_access(self, actor: AuthenticatedPrincipal, account_id: UUID, password: str, correlation_id: UUID | None = None) -> None:
        self._require(actor, "user.reset_access")
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "user.reset_access")
            if not self.repository.set_password(connection, account_id, self.passwords.hash(password)):
                raise AuthorizationError("Acceso no autorizado")
            self._invalidate_accounts(connection, actor, [account_id], correlation)
            self.repository.write_audit_event(connection, actor=actor, action="ACCOUNT_RESET_ACCESS", resource_type="USER", resource_identifier=str(account_id), result="SUCCESS", correlation_id=correlation)

    def assign_roles(self, actor: AuthenticatedPrincipal, account_id: UUID, roles: frozenset[str], correlation_id: UUID | None = None) -> None:
        self._require(actor, "role.assign")
        if actor.account_id == account_id or not roles or not roles.issubset(KNOWN_ROLES):
            raise AuthorizationError("Acceso no autorizado")
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "role.assign")
            if self.repository.account_by_id(connection, account_id, lock=True) is None:
                raise AuthorizationError("Acceso no autorizado")
            if "TI" not in roles and self.repository.is_last_active_ti(connection, account_id):
                raise ValidationError("La operación no está permitida")
            self.repository.set_roles(connection, account_id, roles)
            self._invalidate_accounts(connection, actor, [account_id], correlation)
            self.repository.write_audit_event(connection, actor=actor, action="ROLE_ASSIGNMENT", resource_type="USER", resource_identifier=str(account_id), result="SUCCESS", correlation_id=correlation)

    def set_scope_grant(self, actor: AuthenticatedPrincipal, role_id: str, source_family: str, active: bool, correlation_id: UUID | None = None) -> None:
        self._require(actor, "document_acl.manage")
        if role_id not in KNOWN_ROLES or source_family not in KNOWN_FAMILIES:
            raise ValidationError("Ámbito documental no válido")
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "document_acl.manage")
            self.repository.set_scope_grant(connection, role_id, source_family, active)
            self._invalidate_accounts(connection, actor, self.repository.active_accounts_for_role(connection, role_id), correlation)
            self.repository.write_audit_event(connection, actor=actor, action="DOCUMENT_SCOPE_GRANT", resource_type="ROLE", resource_identifier=role_id, result="SUCCESS", correlation_id=correlation)

    def set_document_exception(self, actor: AuthenticatedPrincipal, *, account_id: UUID | None, role_id: str | None, document_id: str, decision: str, active: bool, correlation_id: UUID | None = None) -> None:
        self._require(actor, "document_acl.manage")
        if not document_id or (account_id is None) == (role_id is None):
            raise ValidationError("Excepción documental no válida")
        correlation = correlation_id or uuid4()
        with self.repository.transaction() as connection:
            actor = self._authoritative_principal(connection, actor, "document_acl.manage")
            self.repository.set_document_exception(connection, account_id=account_id, role_id=role_id, document_id=document_id, decision=decision, active=active)
            impacted = [account_id] if account_id else self.repository.active_accounts_for_role(connection, str(role_id))
            self._invalidate_accounts(connection, actor, [item for item in impacted if item is not None], correlation)
            self.repository.write_audit_event(connection, actor=actor, action="DOCUMENT_EXCEPTION", resource_type="DOCUMENT", resource_identifier=document_id, result="SUCCESS", correlation_id=correlation)

    def document_authorization(self, context: AuthorizationContext, *, document_id: str, source_family: str) -> DocumentAuthorizationDecision:
        self._require(context.principal, "document.query")
        with self.repository.transaction() as connection:
            principal = self._authoritative_principal(connection, context.principal, "document.query")
            decisions, granted_families, _ = self.repository.document_rules(connection, principal.account_id, principal.roles, document_id)
        scope = AuthorizedDocumentScope(principal.account_id, principal.roles, frozenset(granted_families))
        if "DENY" in decisions:
            result = DocumentAuthorizationResult.EXPLICIT_DENY
        elif "ALLOW" in decisions or scope.includes_family(source_family):
            result = DocumentAuthorizationResult.PERMIT
        else:
            result = DocumentAuthorizationResult.DEFAULT_DENY
        return DocumentAuthorizationDecision(result, scope, principal.authorization_version)
