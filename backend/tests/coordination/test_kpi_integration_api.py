from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app


def test_scheduled_kpi_endpoint_is_the_only_coordination_kpi_trigger() -> None:
    client = TestClient(app)
    paths = client.get("/openapi.json").json()["paths"]
    kpi_paths = [path for path in paths if path.startswith("/api/coordination/") and "kpi" in path]
    assert kpi_paths == ["/api/coordination/kpi-recalculations/scheduled"]


def test_scheduled_operation_identity_is_stable_for_one_data_interval() -> None:
    from datetime import UTC, datetime

    from app.coordination.kpi_integration import derive_scheduled_operation_id

    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = datetime(2026, 5, 2, tzinfo=UTC)
    first = derive_scheduled_operation_id(dag_id="controlled_ingestion", interval_start=start, interval_end=end)
    assert isinstance(first, UUID)
    assert first == derive_scheduled_operation_id(dag_id="controlled_ingestion", interval_start=start, interval_end=end)
    assert first != derive_scheduled_operation_id(
        dag_id="controlled_ingestion", interval_start=end, interval_end=datetime(2026, 5, 3, tzinfo=UTC)
    )
