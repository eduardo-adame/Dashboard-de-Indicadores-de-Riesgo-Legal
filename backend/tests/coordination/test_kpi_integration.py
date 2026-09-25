from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

from app.coordination.kpi_integration import (
    canonical_requests_for_projection,
    derive_reinjection_operation_id,
)
from app.coordination.kpi_repository import KpiIntegrationRepository
from app.projection.models import ProjectionResult


def _result(*, entity_type: str = "CONTRATO", business_id: str = "C-1") -> ProjectionResult:
    return ProjectionResult(entity_type, business_id, "INCORPORADO", bytes(32), uuid4(), uuid4(), uuid4(), True)


def test_contract_without_fecha_firma_does_not_create_rc01_request(monkeypatch) -> None:
    result = _result()
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(lambda *_args, **_kwargs: {"applied_at": datetime(2026, 4, 5, tzinfo=UTC)}))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(lambda *_args, **_kwargs: None))
    requests = canonical_requests_for_projection(object(), (result,))
    assert [request.kpi_codes for request in requests] == [("KPI-RC-03",)]


def test_contract_without_fecha_firma_still_creates_rc03_snapshot_request(monkeypatch) -> None:
    result = _result()
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(lambda *_args, **_kwargs: {"applied_at": datetime(2026, 4, 5, tzinfo=UTC)}))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(lambda *_args, **_kwargs: None))
    request = canonical_requests_for_projection(object(), (result,))[0]
    assert request.period_start == date(2026, 4, 1)
    assert request.period_end == date(2026, 4, 30)
    assert request.as_of_date == date(2026, 4, 5)


def test_multi_record_same_period_deduplicates_canonical_request(monkeypatch) -> None:
    results = (_result(business_id="C-1"), _result(business_id="C-2"))
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(lambda *_args, **_kwargs: {"applied_at": datetime(2026, 4, 5, tzinfo=UTC)}))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(lambda *_args, **_kwargs: date(2026, 4, 5)))
    requests = canonical_requests_for_projection(object(), results)
    assert len(requests) == 1
    assert requests[0].kpi_codes == ("KPI-RC-01", "KPI-RC-03")


def test_multi_record_different_periods_keep_distinct_canonical_requests(monkeypatch) -> None:
    results = (_result(business_id="C-1"), _result(business_id="C-2"))
    values = iter((datetime(2026, 4, 5, tzinfo=UTC), datetime(2026, 5, 5, tzinfo=UTC)))
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(lambda *_args, **_kwargs: {"applied_at": next(values)}))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(lambda *_args, **_kwargs: date(2026, 1, 15)))
    requests = canonical_requests_for_projection(object(), results)
    assert len(requests) == 3
    assert {request.period_start for request in requests} == {date(2026, 1, 1), date(2026, 4, 1), date(2026, 5, 1)}


def test_reinjection_identity_is_stable_for_the_same_corrected_candidate() -> None:
    item_id = uuid4()
    assert derive_reinjection_operation_id(item_id, {"ID_Contrato": "C-1"}) == derive_reinjection_operation_id(item_id, {"ID_Contrato": "C-1"})
    assert derive_reinjection_operation_id(item_id, {"ID_Contrato": "C-1"}) != derive_reinjection_operation_id(item_id, {"ID_Contrato": "C-2"})
