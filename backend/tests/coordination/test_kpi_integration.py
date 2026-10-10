from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

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


@pytest.mark.parametrize(
    "entity_type, monthly_code",
    (("CONTRATO", "KPI-RC-01"), ("LITIGIO", "KPI-LI-05"),
     ("INCIDENTE", "KPI-CN-03"), ("ASUNTO", "KPI-EO-01")),
)
@pytest.mark.parametrize(
    "business_date, period_start, period_end, expected_cut",
    ((date(2026, 4, 5), date(2026, 4, 1), date(2026, 4, 30), date(2026, 4, 10)),
     (date(2026, 3, 5), date(2026, 3, 1), date(2026, 3, 31), date(2026, 3, 31)),
     (date(2026, 5, 5), date(2026, 5, 1), date(2026, 5, 31), date(2026, 5, 1))),
)
def test_monthly_request_uses_durable_cut_without_changing_business_period(
    monkeypatch, entity_type, monthly_code, business_date, period_start, period_end, expected_cut,
) -> None:
    """SRS_REQUIRED: una incorporación tardía debe actualizar su observación mensual."""
    result = _result(entity_type=entity_type)
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(
        lambda *_args, **_kwargs: {"applied_at": datetime(2026, 4, 10, tzinfo=UTC)}
    ))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(
        lambda *_args, **_kwargs: business_date
    ))
    requests = canonical_requests_for_projection(object(), (result,))
    request = next(request for request in requests if monthly_code in request.kpi_codes)
    assert (request.period_start, request.period_end) == (period_start, period_end)
    assert request.as_of_date == expected_cut
    assert request.period_start <= request.as_of_date <= request.period_end


def test_durable_cut_uses_utc_calendar_day(monkeypatch) -> None:
    """ROBUSTNESS: un offset local no cambia el día UTC de la aplicación."""
    result = _result(entity_type="LITIGIO")
    applied_at = datetime(2026, 4, 9, 23, 30, tzinfo=timezone(timedelta(hours=-6)))
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(
        lambda *_args, **_kwargs: {"applied_at": applied_at}
    ))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(
        lambda *_args, **_kwargs: date(2026, 4, 5)
    ))
    requests = canonical_requests_for_projection(object(), (result,))
    assert len(requests) == 1
    assert requests[0].kpi_codes == ("KPI-LI-01", "KPI-LI-05")
    assert requests[0].as_of_date == date(2026, 4, 10)


def test_different_business_days_share_one_durable_monthly_cut(monkeypatch) -> None:
    """SRS_REQUIRED: el mismo periodo no genera solicitudes por cada día de negocio."""
    results = (_result(entity_type="LITIGIO", business_id="L-1"),
               _result(entity_type="LITIGIO", business_id="L-2"))
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(
        lambda *_args, **_kwargs: {"applied_at": datetime(2026, 4, 10, tzinfo=UTC)}
    ))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(
        lambda *_args, business_id, **_kwargs: date(2026, 4, 5 if business_id == "L-1" else 7)
    ))
    requests = canonical_requests_for_projection(object(), results)
    assert len(requests) == 1
    assert requests[0].kpi_codes == ("KPI-LI-01", "KPI-LI-05")
    assert requests[0].as_of_date == date(2026, 4, 10)


def test_projection_without_relevant_change_does_not_request_kpis(monkeypatch) -> None:
    """SRS_REQUIRED: el reenvío idempotente no dispara recálculos adicionales."""
    from dataclasses import replace

    result = replace(_result(), result="IDEMPOTENTE", recalculation_required=False)

    def unexpected_reference(*_args, **_kwargs):
        pytest.fail("Una proyección sin cambio no debe consultar ni recalcular KPI")

    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(unexpected_reference))
    assert canonical_requests_for_projection(object(), (result,)) == ()


@pytest.mark.parametrize("entity_type, snapshot_code", (
    ("CONTRATO", "KPI-RC-03"), ("LITIGIO", "KPI-LI-01"), ("OBLIGACION", "KPI-CN-02"),
))
def test_snapshot_kpi_preserves_application_month_and_cut(monkeypatch, entity_type, snapshot_code) -> None:
    """SRS_REQUIRED: las métricas snapshot conservan su corte de aplicación."""
    monkeypatch.setattr(KpiIntegrationRepository, "projection_reference", staticmethod(
        lambda *_args, **_kwargs: {"applied_at": datetime(2026, 4, 10, tzinfo=UTC)}
    ))
    monkeypatch.setattr(KpiIntegrationRepository, "business_temporal_value", staticmethod(
        lambda *_args, **_kwargs: date(2026, 1, 5)
    ))
    requests = canonical_requests_for_projection(object(), (_result(entity_type=entity_type),))
    request = next(request for request in requests if snapshot_code in request.kpi_codes)
    assert request.period_start == date(2026, 4, 1)
    assert request.period_end == date(2026, 4, 30)
    assert request.as_of_date == date(2026, 4, 10)
