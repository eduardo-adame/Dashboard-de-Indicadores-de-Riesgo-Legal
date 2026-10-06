"""Registro aditivo de rutas y preservación de fronteras de autenticación."""
from fastapi.testclient import TestClient
import pytest

from app.main import app


def test_routes_are_registered_once():
    expected = {
        ("GET", "/api/audit/events"),
        ("GET", "/api/dashboard/kpis"),
        ("GET", "/api/dashboard/analysis"),
        ("GET", "/api/technical/kpis/KPI-CD-03"),
        ("GET", "/api/validation/quarantine"),
        ("POST", "/api/validation/quarantine/{item_id}/discard"),
        ("PATCH", "/api/validation/quarantine/{item_id}/reinject"),
        ("GET", "/api/documents/ocr"),
        ("POST", "/api/documents/{document_id}/ocr/reprocess"),
    }
    routes = [(method, route.path) for route in app.routes
              for method in getattr(route, "methods", ())]
    for identity in expected:
        assert routes.count(identity) == 1
    schema = app.openapi()
    for method, path in expected:
        assert method.lower() in schema["paths"][path]


@pytest.mark.parametrize("path", [
    "/api/audit/events", "/api/dashboard/kpis", "/api/dashboard/analysis",
    "/api/technical/kpis/KPI-CD-03", "/api/validation/quarantine", "/api/documents/ocr",
])
def test_new_read_routes_require_authentication(path):
    assert TestClient(app).get(path).status_code == 401


def test_audit_does_not_expose_mutable_routes():
    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/audit"):
            assert set(route.methods) == {"GET"}


def test_existing_health_and_cors_are_preserved():
    schema = app.openapi()
    for path in ("/health", "/health/ready", "/health/embeddings"):
        assert "get" in schema["paths"][path]
    assert any(middleware.cls.__name__ == "CORSMiddleware" for middleware in app.user_middleware)
