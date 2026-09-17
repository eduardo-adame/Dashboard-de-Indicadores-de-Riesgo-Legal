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
