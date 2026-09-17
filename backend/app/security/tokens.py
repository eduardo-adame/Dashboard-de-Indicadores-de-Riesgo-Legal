"""Emisión y validación estricta de JWT HS256."""
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from app.security.models import AuthenticationError


class JwtService:
    """Keyring local con allowlist fija de algoritmo HS256."""

    algorithm = "HS256"

    def __init__(self, *, issuer: str, audience: str, keyring: dict[str, str], active_kid: str) -> None:
        self.issuer = issuer
        self.audience = audience
        self.keyring = {kid: self._decode_secret(secret) for kid, secret in keyring.items()}
        if not active_kid or active_kid not in self.keyring:
            raise ValueError("La clave activa no está configurada")
        self.active_kid = active_kid

    @staticmethod
    def _decode_secret(value: str) -> bytes:
        try:
            padded = value + "=" * (-len(value) % 4)
            secret = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:
            raise ValueError("Cada clave JWT debe usar base64url") from exc
        if len(secret) < 32:
            raise ValueError("Cada clave JWT debe tener al menos 256 bits de entropía")
        return secret

    def issue(self, *, account_id: UUID, session_id: UUID, authorization_version: int, now: datetime | None = None) -> str:
        issued_at = now or datetime.now(UTC)
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": str(account_id),
            "sid": str(session_id),
            "av": authorization_version,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": issued_at + timedelta(minutes=30),
        }
        return jwt.encode(claims, self.keyring[self.active_kid], algorithm=self.algorithm, headers={"kid": self.active_kid})

    def decode(self, token: str) -> dict[str, object]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != self.algorithm:
                raise AuthenticationError()
            kid = header.get("kid")
            if not isinstance(kid, str) or kid not in self.keyring:
                raise AuthenticationError()
            claims = jwt.decode(
                token,
                self.keyring[kid],
                algorithms=[self.algorithm],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["iss", "aud", "sub", "sid", "av", "iat", "nbf", "exp"]},
            )
            UUID(str(claims["sub"]))
            UUID(str(claims["sid"]))
            if not isinstance(claims["av"], int) or claims["av"] <= 0:
                raise AuthenticationError()
            return claims
        except (jwt.PyJWTError, KeyError, TypeError, ValueError, AuthenticationError) as exc:
            raise AuthenticationError("Token no válido") from exc
