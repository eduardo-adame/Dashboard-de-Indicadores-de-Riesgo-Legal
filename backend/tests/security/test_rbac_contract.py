"""Matriz de permisos fijos y default deny."""
from __future__ import annotations

import pytest

from app.security.models import KNOWN_PERMISSIONS, ROLE_PERMISSIONS


@pytest.mark.contract
def test_role_permission_matrix_matches_the_mvp_contract() -> None:
    assert ROLE_PERMISSIONS["JURIDICO"] == {"dashboard.read", "kpi.read", "document.query"}
    assert "audit.read.all" not in ROLE_PERMISSIONS["ANALISTA"]
    assert {"user.create", "user.update", "user.disable", "user.reset_access", "role.assign", "document_acl.manage", "technical_config.manage"}.issubset(ROLE_PERMISSIONS["TI"])
    assert "permission.create" not in KNOWN_PERMISSIONS


@pytest.mark.contract
def test_document_management_capability_is_analista_and_ti_only() -> None:
    assert "document.manage" in ROLE_PERMISSIONS["ANALISTA"]
    assert "document.manage" in ROLE_PERMISSIONS["TI"]
    assert "document.manage" not in ROLE_PERMISSIONS["JURIDICO"]


@pytest.mark.contract
def test_automatic_and_interactive_document_authorization_are_separate() -> None:
    from app.documents.service import MANAGEMENT_CAPABILITY

    # Gestión/reproceso interactivo: capacidad dedicada.
    assert MANAGEMENT_CAPABILITY == "document.manage"
    assert {"ANALISTA", "TI"}.issubset({role for role, perms in ROLE_PERMISSIONS.items() if "document.manage" in perms})
    assert "document.manage" not in ROLE_PERMISSIONS["JURIDICO"]

    # Pipeline automático: usa ingest.execute; no depende de document.manage.
    assert "ingest.execute" in ROLE_PERMISSIONS["ANALISTA"]
    assert "ingest.execute" not in ROLE_PERMISSIONS["JURIDICO"]
