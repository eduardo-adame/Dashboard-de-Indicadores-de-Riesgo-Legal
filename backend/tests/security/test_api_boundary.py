"""Pruebas de protección de la frontera HTTP."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.contract
def test_protected_endpoints_reject_direct_calls_without_credentials(client: TestClient) -> None:
    responses = (
        client.get("/api/auth/me"),
        client.post("/api/auth/logout"),
        client.post("/api/security/users", json={"username": "new", "display_name": "Nuevo", "password": "contraseña válida 123", "roles": ["TI"]}),
    )
    for response in responses:
        assert response.status_code == 401
        assert response.json()["detail"] == "Credenciales o sesión no válidas"


@pytest.mark.contract
def test_public_health_remains_available(client: TestClient) -> None:
    assert client.get("/health").status_code == 200
