from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.coordination.api import ScheduledProactiveRequest
from app.main import app


def test_scheduled_proactive_endpoint_is_single_application_boundary() -> None:
    paths = TestClient(app).get("/openapi.json").json()["paths"]
    proactive = [path for path in paths if "proactive-analysis" in path]
    assert proactive == ["/api/coordination/proactive-analysis/scheduled"]


def test_caller_cannot_inject_analytical_contract_fields() -> None:
    with pytest.raises(ValidationError):
        ScheduledProactiveRequest.model_validate(
            {
                "data_interval_start": "2026-05-01T00:00:00Z",
                "data_interval_end": "2026-05-02T00:00:00Z",
                "fingerprint": "caller-controlled",
            }
        )


def test_scheduled_proactive_endpoint_requires_authentication() -> None:
    response = TestClient(app).post(
        "/api/coordination/proactive-analysis/scheduled",
        json={
            "data_interval_start": "2026-05-01T00:00:00Z",
            "data_interval_end": "2026-05-02T00:00:00Z",
        },
    )
    assert response.status_code in {401, 403}


def test_request_contract_accepts_only_operational_identity() -> None:
    request = ScheduledProactiveRequest(
        data_interval_start=datetime(2026, 5, 1, tzinfo=UTC),
        data_interval_end=datetime(2026, 5, 2, tzinfo=UTC),
        correlation_id=str(uuid4()),
    )
    assert set(request.model_dump()) == {
        "data_interval_start",
        "data_interval_end",
        "correlation_id",
    }
