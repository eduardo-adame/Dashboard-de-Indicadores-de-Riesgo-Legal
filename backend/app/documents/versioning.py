"""Reglas de versión documental y protección de escritura obsoleta.

Una versión candidata solo puede sustituir a la vigente cuando termina su
procesamiento. La activación comprueba que la versión activa observada al iniciar
el procesamiento sigue siendo la vigente; si cambió, la activación se rechaza.
"""
from __future__ import annotations

from uuid import UUID

from app.documents.models import (
    DocumentVersionState,
    StaleWriteError,
    VersionNotReadyError,
)


def ensure_candidate_ready(state: DocumentVersionState) -> None:
    """Comprueba que la versión candidata terminó su procesamiento."""
    if state != DocumentVersionState.LISTA:
        raise VersionNotReadyError(f"la versión candidata no está lista: {state.value}")


def expected_active_matches(expected_active_version_id: UUID | None, current_active_version_id: UUID | None) -> bool:
    """Indica si la versión activa observada sigue siendo la vigente."""
    return expected_active_version_id == current_active_version_id


def assert_expected_active(expected_active_version_id: UUID | None, current_active_version_id: UUID | None) -> None:
    """Rechaza la activación cuando la versión vigente cambió."""
    if not expected_active_matches(expected_active_version_id, current_active_version_id):
        raise StaleWriteError("la versión activa cambió desde el inicio del procesamiento")
