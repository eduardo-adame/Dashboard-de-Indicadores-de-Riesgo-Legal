"""Hashing de contraseñas con parámetros seguros para el MVP."""
from __future__ import annotations

from hmac import compare_digest

from pwdlib import PasswordHash

from app.security.models import ValidationError


class PasswordService:
    """Aplica la política de longitud y Argon2id sin reglas de composición."""

    def __init__(self) -> None:
        self._hasher = PasswordHash.recommended()
        self._dummy_hash = self._hasher.hash("x" * 32)

    @staticmethod
    def validate(password: str) -> None:
        if not 12 <= len(password) <= 128:
            raise ValidationError("La contraseña no cumple la política de longitud")

    def hash(self, password: str) -> str:
        self.validate(password)
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str | None) -> bool:
        """Comprueba también un hash ficticio cuando la cuenta no existe."""
        candidate = password_hash or self._dummy_hash
        try:
            verified = self._hasher.verify(password, candidate)
        except Exception:  # Un hash corrupto se trata como credencial inválida.
            verified = False
        return bool(verified and password_hash and compare_digest("1", "1"))
