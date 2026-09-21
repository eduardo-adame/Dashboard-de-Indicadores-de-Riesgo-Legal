"""Pruebas del bootstrap local y de una sola ejecución de la primera TI."""
from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest

from app.security import bootstrap
from app.security.models import BootstrapAlreadyCompletedError, ValidationError
from app.security.service import SecurityService
from app.security.tokens import JwtService


class BootstrapRepository:
    def __init__(self) -> None:
        self.accounts: dict[UUID, dict[str, object]] = {}
        self.roles: dict[UUID, frozenset[str]] = {}
        self.audit: list[dict[str, object]] = []
        self.lock_calls = 0
        self.fail_audit = False

    @contextmanager
    def transaction(self):
        snapshot = copy.deepcopy((self.accounts, self.roles, self.audit))
        try:
            yield self
        except Exception:
            self.accounts, self.roles, self.audit = snapshot
            raise

    def acquire_initial_admin_lock(self, connection) -> None:
        self.lock_calls += 1

    def has_active_ti_account(self, connection) -> bool:
        return any(self.roles.get(account_id) == frozenset({"TI"}) and account["state"] == "ACTIVE" for account_id, account in self.accounts.items())

    def create_account(self, connection, *, username, display_name, password_hash):
        account_id = uuid4()
        self.accounts[account_id] = {"username": username, "display_name": display_name, "password_hash": password_hash, "state": "ACTIVE", "authorization_version": 1}
        return account_id

    def set_roles(self, connection, account_id, roles) -> None:
        self.roles[account_id] = roles

    def write_audit_event(self, connection, **event) -> None:
        if self.fail_audit:
            raise RuntimeError("audit unavailable")
        self.audit.append(event)


def service(repository: BootstrapRepository) -> SecurityService:
    key = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
    return SecurityService(repository, JwtService(issuer="issuer", audience="audience", keyring={"current": key}, active_kid="current"))


def test_bootstrap_creates_one_active_ti_with_normal_password_policy_and_audit() -> None:
    repository = BootstrapRepository()
    account_id = service(repository).bootstrap_first_ti(username="admin", password="contraseña válida 123")
    assert repository.roles[account_id] == frozenset({"TI"})
    assert repository.accounts[account_id]["authorization_version"] == 1
    assert "contraseña válida 123" not in str(repository.accounts[account_id]["password_hash"])
    assert repository.audit[0]["action"] == "SECURITY_BOOTSTRAP"
    assert repository.audit[0]["actor"] is None
    assert repository.audit[0]["process_identifier"] == "security-bootstrap"
    assert repository.lock_calls == 1


@pytest.mark.parametrize("username,password", [(" ", "contraseña válida 123"), ("admin", "corta")])
def test_bootstrap_rejects_invalid_normal_account_input(username: str, password: str) -> None:
    with pytest.raises(ValidationError):
        service(BootstrapRepository()).bootstrap_first_ti(username=username, password=password)


def test_bootstrap_repeated_attempt_is_fail_closed_without_mutation() -> None:
    repository = BootstrapRepository()
    secured = service(repository)
    account_id = secured.bootstrap_first_ti(username="admin", password="contraseña válida 123")
    with pytest.raises(BootstrapAlreadyCompletedError, match="BOOTSTRAP_ALREADY_COMPLETED"):
        secured.bootstrap_first_ti(username="other", password="otra contraseña válida 123")
    assert list(repository.accounts) == [account_id]
    assert len(repository.audit) == 1


def test_historical_inactive_ti_is_not_modified_or_reactivated() -> None:
    repository = BootstrapRepository()
    historical_id = uuid4()
    repository.accounts[historical_id] = {
        "username": "historical-admin",
        "display_name": "Historical",
        "password_hash": "unusable",
        "state": "DISABLED",
        "authorization_version": 3,
    }
    repository.roles[historical_id] = frozenset({"TI"})

    created_id = service(repository).bootstrap_first_ti(
        username="first-active-admin", password="contraseña válida 123"
    )

    assert repository.accounts[historical_id]["state"] == "DISABLED"
    assert repository.accounts[historical_id]["authorization_version"] == 3
    assert repository.roles[created_id] == frozenset({"TI"})


def test_bootstrap_audit_failure_rolls_back_account_and_role() -> None:
    repository = BootstrapRepository()
    repository.fail_audit = True
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service(repository).bootstrap_first_ti(username="admin", password="contraseña válida 123")
    assert repository.accounts == {}
    assert repository.roles == {}
    assert repository.audit == []


def test_cli_fails_closed_when_bootstrap_is_disabled_or_incomplete(monkeypatch) -> None:
    class DisabledSettings:
        security_bootstrap_enabled = False

    monkeypatch.setattr(bootstrap, "get_settings", lambda: DisabledSettings())
    assert bootstrap.main() == 2

    class IncompleteSettings:
        security_bootstrap_enabled = True
        security_bootstrap_username = None
        security_bootstrap_password = None

    monkeypatch.setattr(bootstrap, "get_settings", lambda: IncompleteSettings())
    assert bootstrap.main() == 2
