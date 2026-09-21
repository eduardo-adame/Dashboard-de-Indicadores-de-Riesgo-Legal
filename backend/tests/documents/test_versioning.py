"""Pruebas de reglas de versión documental y protección de escritura obsoleta."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.documents.models import DocumentVersionState, StaleWriteError, VersionNotReadyError
from app.documents.versioning import assert_expected_active, ensure_candidate_ready, expected_active_matches


def test_ready_candidate_passes() -> None:
    ensure_candidate_ready(DocumentVersionState.LISTA)


def test_non_ready_candidate_is_rejected() -> None:
    for state in (DocumentVersionState.PENDIENTE, DocumentVersionState.PROCESANDO, DocumentVersionState.FALLIDA):
        with pytest.raises(VersionNotReadyError):
            ensure_candidate_ready(state)


def test_expected_active_matches_same_version() -> None:
    version = uuid4()
    assert expected_active_matches(version, version) is True


def test_stale_candidate_is_rejected() -> None:
    observed = uuid4()
    newer = uuid4()
    with pytest.raises(StaleWriteError):
        assert_expected_active(observed, newer)


def test_first_activation_from_no_active_version_is_allowed() -> None:
    assert_expected_active(None, None)


def test_stale_from_no_active_to_active_is_rejected() -> None:
    with pytest.raises(StaleWriteError):
        assert_expected_active(None, uuid4())
