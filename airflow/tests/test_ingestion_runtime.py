from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


_RUNTIME = Path(__file__).resolve().parents[1] / "dags" / "ingestion_runtime.py"


def _module():
    spec = importlib.util.spec_from_file_location("ingestion_runtime", _RUNTIME)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_airflow_graph_is_ingestion_then_kpi_then_proactive() -> None:
    runtime = _module()
    assert runtime.dag.dag_id == "controlled_ingestion"
    assert set(runtime.dag.task_ids) == {
        "run_controlled_ingestion",
        "request_scheduled_kpi_recalculation",
        "request_scheduled_proactive_analysis",
    }
    assert runtime.dag.get_task("request_scheduled_kpi_recalculation").upstream_task_ids == {"run_controlled_ingestion"}
    assert runtime.dag.get_task("request_scheduled_proactive_analysis").upstream_task_ids == {"request_scheduled_kpi_recalculation"}


def test_interval_correlation_is_stable() -> None:
    runtime = _module()
    interval = {
        "data_interval_start": SimpleNamespace(in_timezone=lambda _zone: SimpleNamespace(isoformat=lambda: "2026-05-01T00:00:00+00:00")),
        "data_interval_end": SimpleNamespace(in_timezone=lambda _zone: SimpleNamespace(isoformat=lambda: "2026-05-02T00:00:00+00:00")),
    }
    assert runtime._correlation_id(interval) == runtime._correlation_id(interval)


class _Response:
    def __init__(self, state: str, *, status_error: Exception | None = None) -> None:
        self.state = state
        self.status_error = status_error

    def raise_for_status(self) -> None:
        if self.status_error:
            raise self.status_error

    def json(self):
        return {"state": self.state}


def _context():
    instant = SimpleNamespace(
        in_timezone=lambda _zone: SimpleNamespace(
            isoformat=lambda: "2026-05-01T00:00:00+00:00"
        )
    )
    end = SimpleNamespace(
        in_timezone=lambda _zone: SimpleNamespace(
            isoformat=lambda: "2026-05-02T00:00:00+00:00"
        )
    )
    return {"data_interval_start": instant, "data_interval_end": end}


def _prepare_requests(monkeypatch, runtime, states):
    responses = iter(states)
    calls = []
    monkeypatch.setattr(runtime, "_login", lambda _endpoint: ({"Authorization": "test"}, "pipeline"))

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/api/auth/logout"):
            return _Response("LOGOUT")
        return _Response(next(responses))

    monkeypatch.setattr(runtime.requests, "post", post)
    return calls


def test_proactive_runs_only_after_kpi_terminal_success(monkeypatch) -> None:
    runtime = _module()
    calls = _prepare_requests(monkeypatch, runtime, ["COMPLETED", "COMPLETED"])
    runtime.request_scheduled_kpi_recalculation(**_context())
    runtime.request_scheduled_proactive_analysis(**_context())
    application_calls = [url for url, _ in calls if "/api/coordination/" in url]
    assert application_calls == [
        "http://backend:8000/api/coordination/kpi-recalculations/scheduled",
        "http://backend:8000/api/coordination/proactive-analysis/scheduled",
    ]


def test_proactive_does_not_run_while_kpi_job_started(monkeypatch) -> None:
    runtime = _module()
    calls = _prepare_requests(monkeypatch, runtime, ["STARTED"])
    with pytest.raises(RuntimeError, match="KPI"):
        runtime.request_scheduled_kpi_recalculation(**_context())
    assert not any("proactive-analysis" in url for url, _ in calls)


def test_proactive_does_not_run_after_kpi_failure(monkeypatch) -> None:
    runtime = _module()
    calls = _prepare_requests(monkeypatch, runtime, ["FAILED"])
    with pytest.raises(RuntimeError, match="KPI"):
        runtime.request_scheduled_kpi_recalculation(**_context())
    assert not any("proactive-analysis" in url for url, _ in calls)


def test_kpi_no_relevant_work_is_valid_predecessor_terminal_state(monkeypatch) -> None:
    runtime = _module()
    _prepare_requests(monkeypatch, runtime, ["NO_RELEVANT_WORK"])
    runtime.request_scheduled_kpi_recalculation(**_context())


def test_airflow_retry_preserves_logical_identity(monkeypatch) -> None:
    runtime = _module()
    calls = _prepare_requests(monkeypatch, runtime, ["COMPLETED", "COMPLETED"])
    runtime.request_scheduled_proactive_analysis(**_context())
    runtime.request_scheduled_proactive_analysis(**_context())
    payloads = [kwargs["json"] for url, kwargs in calls if "proactive-analysis" in url]
    assert payloads[0] == payloads[1]


def test_airflow_uses_application_boundary_without_sql() -> None:
    source = _RUNTIME.read_text(encoding="utf-8")
    assert "/api/coordination/proactive-analysis/scheduled" in source
    assert "psycopg" not in source
    assert "proactive_input_snapshot" not in source
