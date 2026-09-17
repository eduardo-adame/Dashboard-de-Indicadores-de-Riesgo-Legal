"""Flujos funcionales de autenticación, autorización y límites de retrieval."""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.security.models import (
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthorizationContext,
    AuthorizationError,
    DocumentAuthorizationResult,
)
from app.security.service import SecurityService
from app.security.tokens import JwtService


class MemorySecurityRepository:
    """Equivalente persistente en memoria para probar casos de uso completos."""

    def __init__(self) -> None:
        self.accounts: dict[UUID, dict[str, object]] = {}
        self.sessions: dict[UUID, dict[str, object]] = {}
        self.roles: dict[UUID, frozenset[str]] = {}
        self.grants: set[tuple[str, str]] = set()
        self.exceptions: dict[tuple[str, str, str], str] = {}
        self.audit: list[dict[str, object]] = []
        self.fail_audit = False

    @contextmanager
    def transaction(self):
        state = copy.deepcopy((self.accounts, self.sessions, self.roles, self.grants, self.exceptions, self.audit))
        try:
            yield self
        except Exception:
            self.accounts, self.sessions, self.roles, self.grants, self.exceptions, self.audit = state
            raise

    def add_account(self, username: str, *, roles: frozenset[str] = frozenset({"TI"}), state: str = "ACTIVE") -> UUID:
        account_id = uuid4()
        self.accounts[account_id] = {"id": account_id, "username": username, "display_name": username, "password_hash": "", "state": state, "authorization_version": 1}
        self.roles[account_id] = roles
        return account_id

    def _principal(self, account_id: UUID, session_id: UUID) -> AuthenticatedPrincipal | None:
        account, session = self.accounts.get(account_id), self.sessions.get(session_id)
        if not account or not session or account["state"] != "ACTIVE" or session["state"] != "ACTIVE" or session["expires_at"] <= datetime.now(UTC):
            return None
        version = int(account["authorization_version"])
        if int(session["authorization_version"]) != version:
            return None
        roles = self.roles[account_id]
        permissions = set()
        from app.security.models import ROLE_PERMISSIONS
        for role in roles:
            permissions.update(ROLE_PERMISSIONS[role])
        return AuthenticatedPrincipal(account_id, session_id, str(account["username"]), version, roles, frozenset(permissions))

    def account_by_username(self, connection, username):
        return next((account for account in self.accounts.values() if account["username"] == username), None)

    def principal_for_session(self, connection, account_id, session_id, authorization_version):
        principal = self._principal(account_id, session_id)
        return principal if principal and principal.authorization_version == authorization_version else None

    def _roles_and_permissions(self, connection, account_id):
        principal = self._principal(account_id, next(session_id for session_id, session in self.sessions.items() if session["user_id"] == account_id))
        return principal.roles, principal.permissions

    def create_session(self, connection, *, account_id, authorization_version, token_hash, expires_at, correlation_id):
        session_id = uuid4(); self.sessions[session_id] = {"id": session_id, "user_id": account_id, "refresh_token_sha256": token_hash, "authorization_version": authorization_version, "expires_at": expires_at, "state": "ACTIVE"}; return session_id

    def refresh_session(self, connection, *, token_hash, replacement_hash):
        for session in self.sessions.values():
            if session["refresh_token_sha256"] == token_hash:
                account = self.accounts[session["user_id"]]
                if session["state"] != "ACTIVE" or session["expires_at"] <= datetime.now(UTC) or session["authorization_version"] != account["authorization_version"] or account["state"] != "ACTIVE": return None
                session["refresh_token_sha256"] = replacement_hash
                return {**session, "account_authorization_version": account["authorization_version"]}
        return None

    def invalidate_sessions(self, connection, account_ids):
        affected = [sid for sid, session in self.sessions.items() if session["user_id"] in account_ids and session["state"] == "ACTIVE"]
        for account_id in account_ids: self.accounts[account_id]["authorization_version"] = int(self.accounts[account_id]["authorization_version"]) + 1
        for sid in affected: self.sessions[sid]["state"] = "INVALIDATED"
        return affected

    def invalidate_one_session(self, connection, session_id): self.sessions[session_id]["state"] = "LOGGED_OUT"
    def create_account(self, connection, *, username, display_name, password_hash):
        account_id = self.add_account(username, roles=frozenset({"JURIDICO"})); self.accounts[account_id]["display_name"] = display_name; self.accounts[account_id]["password_hash"] = password_hash; return account_id
    def set_roles(self, connection, account_id, roles): self.roles[account_id] = roles
    def account_by_id(self, connection, account_id, lock=False): return self.accounts.get(account_id)
    def set_account_state(self, connection, account_id, state):
        account = self.accounts.get(account_id)
        if not account or account["state"] == state: return False
        account["state"] = state
        return True
    def is_last_active_ti(self, connection, account_id): return account_id in self.accounts and self.roles[account_id] == frozenset({"TI"}) and sum(self.roles[item] == frozenset({"TI"}) and account["state"] == "ACTIVE" for item, account in self.accounts.items()) == 1
    def active_accounts_for_role(self, connection, role_id): return [account_id for account_id, roles in self.roles.items() if role_id in roles and self.accounts[account_id]["state"] == "ACTIVE"]
    def set_scope_grant(self, connection, role_id, source_family, active):
        key = (role_id, source_family); self.grants = self.grants | {key} if active else self.grants - {key}
    def set_document_exception(self, connection, *, account_id, role_id, document_id, decision, active):
        key = ("user" if account_id else "role", str(account_id or role_id), document_id)
        if active: self.exceptions[key] = decision
        else: self.exceptions.pop(key, None)
    def document_rules(self, connection, account_id, roles, document_id):
        decisions = {decision for (kind, subject, doc), decision in self.exceptions.items() if doc == document_id and ((kind == "user" and subject == str(account_id)) or (kind == "role" and subject in roles))}
        return decisions, {family for role, family in self.grants if role in roles}, set()
    def write_audit_event(self, connection, **kwargs):
        if self.fail_audit: raise RuntimeError("audit unavailable")
        self.audit.append(kwargs)


def make_service(repository: MemorySecurityRepository) -> SecurityService:
    key = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
    return SecurityService(repository, JwtService(issuer="issuer", audience="audience", keyring={"current": key}, active_kid="current"))


def signed_in(repository: MemorySecurityRepository, service: SecurityService, username="ti"):
    account_id = repository.add_account(username); repository.accounts[account_id]["password_hash"] = service.passwords.hash("contraseña válida 123")
    access, refresh = service.login(username, "contraseña válida 123")
    return account_id, service.authenticated_principal(access), refresh


@pytest.mark.contract
def test_login_valid_invalid_unknown_disabled_and_audit_are_safe() -> None:
    repository = MemorySecurityRepository(); service = make_service(repository)
    account_id = repository.add_account("active"); repository.accounts[account_id]["password_hash"] = service.passwords.hash("contraseña válida 123")
    disabled = repository.add_account("disabled", state="DISABLED"); repository.accounts[disabled]["password_hash"] = service.passwords.hash("contraseña válida 123")
    assert service.login("active", "contraseña válida 123")[0]
    for username in ("active", "unknown", "disabled"):
        with pytest.raises(AuthenticationError, match="Credenciales o sesión no válidas"):
            service.login(username, "incorrecta 123") if username == "active" else service.login(username, "contraseña válida 123")
    assert [event["action"] for event in repository.audit].count("AUTH_LOGIN") == 4


@pytest.mark.contract
def test_refresh_rotates_rejects_reuse_revocation_expiry_and_never_extends_absolute_window() -> None:
    repository = MemorySecurityRepository(); service = make_service(repository); _, principal, refresh = signed_in(repository, service)
    original_expiry = repository.sessions[principal.session_id]["expires_at"]
    _, replacement = service.refresh(refresh)
    assert repository.sessions[principal.session_id]["expires_at"] == original_expiry
    with pytest.raises(AuthenticationError): service.refresh(refresh)
    repository.sessions[principal.session_id]["state"] = "INVALIDATED"
    with pytest.raises(AuthenticationError): service.refresh(replacement)
    repository.sessions[principal.session_id]["state"] = "ACTIVE"; repository.sessions[principal.session_id]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(AuthenticationError): service.refresh(replacement)


@pytest.mark.contract
def test_stale_authorization_and_claims_never_override_current_roles() -> None:
    repository = MemorySecurityRepository(); service = make_service(repository); account_id, principal, _ = signed_in(repository, service)
    repository.accounts[account_id]["authorization_version"] = 2
    with pytest.raises(AuthorizationError): service.require_functional_permission(principal, "user.create")


@pytest.mark.contract
def test_last_ti_and_self_role_edit_are_rejected_but_other_user_role_edit_is_allowed() -> None:
    repository = MemorySecurityRepository(); service = make_service(repository); ti_id, principal, _ = signed_in(repository, service)
    with pytest.raises(Exception): service.disable_account(principal, ti_id)
    with pytest.raises(AuthorizationError): service.assign_roles(principal, ti_id, frozenset({"JURIDICO"}))
    other = repository.add_account("other", roles=frozenset({"JURIDICO"})); service.assign_roles(principal, other, frozenset({"ANALISTA"}))
    assert repository.roles[other] == frozenset({"ANALISTA"})
    second = repository.add_account("second", roles=frozenset({"TI"})); service.disable_account(principal, second)
    assert repository.accounts[second]["state"] == "DISABLED"


@pytest.mark.contract
def test_persisted_acl_precedence_and_indistinguishable_denials() -> None:
    repository = MemorySecurityRepository(); service = make_service(repository); account_id, principal, _ = signed_in(repository, service)
    context = AuthorizationContext(principal, uuid4())
    repository.set_scope_grant(None, "TI", "LITIGIOS", True)
    assert service.document_authorization(context, document_id="document-a", source_family="LITIGIOS").permitted
    repository.set_document_exception(None, account_id=account_id, role_id=None, document_id="document-a", decision="ALLOW", active=True)
    assert service.document_authorization(context, document_id="document-a", source_family="CUMPLIMIENTO").permitted
    repository.set_document_exception(None, account_id=None, role_id="TI", document_id="document-a", decision="DENY", active=True)
    assert service.document_authorization(context, document_id="document-a", source_family="LITIGIOS").result is DocumentAuthorizationResult.EXPLICIT_DENY
    assert service.document_authorization(context, document_id="missing", source_family="CUMPLIMIENTO").result == service.document_authorization(context, document_id="restricted", source_family="CUMPLIMIENTO").result


@pytest.mark.contract
def test_security_trimming_scope_reaches_retrieval_before_any_document_is_consumed() -> None:
    repository = MemorySecurityRepository(); service = make_service(repository); _, principal, _ = signed_in(repository, service)
    repository.set_scope_grant(None, "TI", "LITIGIOS", True)
    scope = service.document_authorization(AuthorizationContext(principal, uuid4()), document_id="doc", source_family="LITIGIOS").scope
    seen: list[object] = []
    def retrieval(received_scope):
        seen.append(received_scope); return ["authorized-document"]
    assert retrieval(scope) == ["authorized-document"] and seen == [scope]
    assert not scope.includes_family("CUMPLIMIENTO")


@pytest.mark.contract
def test_required_audit_failure_rolls_back_the_mutation_without_partial_state() -> None:
    repository = MemorySecurityRepository(); service = make_service(repository); _, principal, _ = signed_in(repository, service)
    repository.fail_audit = True
    with pytest.raises(RuntimeError): service.create_account(principal, username="new", display_name="Nuevo", password="contraseña válida 123", roles=frozenset({"JURIDICO"}))
    assert all(account["username"] != "new" for account in repository.accounts.values())
