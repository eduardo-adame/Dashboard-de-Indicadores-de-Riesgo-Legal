"""Pruebas unitarias del perfil JWT y de contraseñas."""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.security.models import AuthenticationError, ValidationError
from app.security.passwords import PasswordService
from app.security.tokens import JwtService


def keyring() -> dict[str, str]:
    return {"current": base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")}


@pytest.mark.contract
def test_jwt_requires_hs256_known_kid_and_required_claims() -> None:
    service = JwtService(issuer="issuer", audience="audience", keyring=keyring(), active_kid="current")
    account_id, session_id = uuid4(), uuid4()
    token = service.issue(account_id=account_id, session_id=session_id, authorization_version=3)
    claims = service.decode(token)
    assert claims["sub"] == str(account_id)
    assert claims["sid"] == str(session_id)
    assert claims["av"] == 3

    base_claims = {"iss": "issuer", "aud": "audience", "sub": str(account_id), "sid": str(session_id), "av": 3, "iat": datetime.now(UTC), "nbf": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=1)}
    hs512 = jwt.encode(base_claims, b"a" * 32, algorithm="HS512", headers={"kid": "current"})
    unknown_kid = jwt.encode(base_claims, b"a" * 32, algorithm="HS256", headers={"kid": "unknown"})
    invalid_audience = jwt.encode({**base_claims, "aud": "other"}, b"a" * 32, algorithm="HS256", headers={"kid": "current"})
    for invalid in (hs512, unknown_kid, invalid_audience):
        with pytest.raises(AuthenticationError):
            service.decode(invalid)


@pytest.mark.contract
def test_jwt_rejects_expired_and_bad_signature() -> None:
    service = JwtService(issuer="issuer", audience="audience", keyring=keyring(), active_kid="current")
    account_id, session_id = uuid4(), uuid4()
    expired = service.issue(account_id=account_id, session_id=session_id, authorization_version=1, now=datetime.now(UTC) - timedelta(hours=1))
    tampered = expired[:-1] + ("a" if expired[-1] != "a" else "b")
    for invalid in (expired, tampered):
        with pytest.raises(AuthenticationError):
            service.decode(invalid)


@pytest.mark.contract
def test_password_policy_uses_argon2id_and_accepts_unicode() -> None:
    passwords = PasswordService()
    password = "Contraseña válida ☕ 123"
    encoded = passwords.hash(password)
    assert encoded.startswith("$argon2id$")
    assert passwords.verify(password, encoded) is True
    assert passwords.verify("otra contraseña", encoded) is False
    assert passwords.verify("otra contraseña", None) is False
    for invalid in ("corta", "x" * 129):
        with pytest.raises(ValidationError):
            passwords.hash(invalid)
