"""Pruebas de los endpoints de salud.

Son pruebas de robustez: las rutas y los códigos de estado son una decisión de
diseño de este servicio, no un contrato externo.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.embeddings.bge_m3 import EmbeddingServiceStatus
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.robustness
def test_health_returns_200(client: TestClient) -> None:
    """El endpoint de liveness responde 200 con cuerpo JSON de estado."""
    response = client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], (int, float))


@pytest.mark.robustness
def test_health_does_not_expose_secrets(client: TestClient) -> None:
    """La respuesta de salud no filtra configuración sensible."""
    body = client.get("/health").json()
    serialized = str(body).lower()
    for forbidden in ("password", "fernet", "api_key", "token"):
        assert forbidden not in serialized


@pytest.mark.robustness
def test_health_does_not_expose_unrelated_business_routes() -> None:
    """Salud no habilita módulos de dominio fuera de seguridad."""
    routes = {getattr(route, "path", "") for route in app.routes}
    business_prefixes = (
        "/ingesta",
        "/documentos",
        "/indicadores",
        "/kpi",
        "/analisis",
        "/busqueda",
        "/rag",
        "/usuarios",
        "/roles",
        "/auditoria",
    )
    for route in routes:
        assert not route.startswith(business_prefixes), (
            f"Ruta de negocio inesperada en este servicio: {route}"
        )


@pytest.mark.robustness
@pytest.mark.requires_db
def test_ready_reports_database_status(client: TestClient) -> None:
    """Readiness informa el estado de la base de datos sin crear estructura.

    Acepta 200 (operativo) o 503 (degradado): se verifica que el contrato de
    respuesta existe y es coherente, no que la base de datos esté disponible en
    la máquina que ejecuta la prueba unitaria.
    """
    response = client.get("/health/ready")
    assert response.status_code in (200, 503)

    body = response.json()
    assert body["status"] in ("ok", "degraded")
    database = body["database"]
    for key in (
        "reachable",
        "pgvector_available_in_image",
        "pgvector_installed",
        "vector_1024_accepted",
    ):
        assert key in database


@pytest.mark.robustness
def test_embeddings_status_does_not_force_load(client: TestClient) -> None:
    """Consultar el estado de embeddings no descarga ni carga el modelo."""
    body = client.get("/health/embeddings").json()
    assert body["shared_instance"] is True
    assert body["load_count"] == 0
    assert body["available"] is False
    assert "cache_dir" in body


@pytest.mark.robustness
def test_embeddings_probe_reports_unavailability_honestly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el modelo no está disponible se reporta la indisponibilidad.

    La respuesta nunca debe presentarse como si el resultado se hubiera generado
    correctamente.
    """
    from app.embeddings import bge_m3

    def _unavailable_probe(self) -> EmbeddingServiceStatus:
        return EmbeddingServiceStatus(
            available=False,
            model_id=self.model_id,
            dimensions=None,
            cache_dir=self.cache_dir,
            cache_dir_is_mount=self.cache_dir_is_mount,
            load_count=self.load_count,
            shared_instance=True,
            error="modelo no disponible en este host",
        )

    monkeypatch.setattr(bge_m3.BgeM3EmbeddingService, "probe", _unavailable_probe)

    response = client.post("/health/embeddings/probe")
    assert response.status_code == 503

    body = response.json()
    assert body["status"] == "unavailable"
    assert body["available"] is False
    assert "no pudo completarse" in body["detail"]
