from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


_RUNTIME = Path(__file__).resolve().parents[1] / "dags" / "ingestion_runtime.py"


def _module():
    spec = importlib.util.spec_from_file_location("ingestion_runtime", _RUNTIME)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controlled_ingestion_dag_has_one_follow_up_kpi_task() -> None:
    runtime = _module()
    assert runtime.dag.dag_id == "controlled_ingestion"
    assert set(runtime.dag.task_ids) == {"run_controlled_ingestion", "request_scheduled_kpi_recalculation"}
    assert runtime.dag.get_task("request_scheduled_kpi_recalculation").upstream_task_ids == {"run_controlled_ingestion"}


def test_interval_correlation_is_stable() -> None:
    runtime = _module()
    interval = {
        "data_interval_start": SimpleNamespace(in_timezone=lambda _zone: SimpleNamespace(isoformat=lambda: "2026-05-01T00:00:00+00:00")),
        "data_interval_end": SimpleNamespace(in_timezone=lambda _zone: SimpleNamespace(isoformat=lambda: "2026-05-02T00:00:00+00:00")),
    }
    assert runtime._correlation_id(interval) == runtime._correlation_id(interval)
