from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.analytics.models import KpiCalculationError, KpiRecalculationContext, KpiRecalculationRequest
from app.analytics.service import KPIRecalculationService, derive_run_operation_id, requested_kpis_for_projection
from app.projection.models import ProjectionResult


class _Repository:
    def __init__(self) -> None:
        self.runs, self.observations = {}, {}
        self.contracts = []; self.litigations = []; self.compliance = []; self.incidents = []; self.matters = []; self.ocr = []
        self.known_incidents = []; self.known_matters = []

    @contextmanager
    def transaction(self): yield self
    def acquire_run_lock(self, _): return None
    def run_for_operation(self, _, operation): return self.runs.get(operation)
    def create_run(self, _, **kwargs):
        run = {"id": uuid4(), "state": "STARTED", "operation_id": kwargs["operation_id"]}; self.runs[kwargs["operation_id"]] = run; return run["id"]
    def restart_failed_run(self, _, run): next(item for item in self.runs.values() if item["id"] == run)["state"] = "STARTED"
    def complete_run(self, _, run): next(item for item in self.runs.values() if item["id"] == run)["state"] = "COMPLETED"
    def fail_run(self, _, run, __): next(item for item in self.runs.values() if item["id"] == run)["state"] = "FAILED"
    def upsert_observation(self, _, observation, __): self.observations[(observation.kpi_code, tuple(sorted(observation.dimensions.items())))] = observation
    def current_observations(self, _, **__): return [{"kpi_code": item.kpi_code, "period_start": item.period_start, "period_end": item.period_end, "dimensions": item.dimensions, "value": item.value, "availability": item.availability, "as_of_date": item.as_of_date} for item in self.observations.values()]
    def contract_rows(self, *_): return self.contracts
    def contracts_snapshot(self, *_): return self.contracts
    def litigation_rows(self, *_): return self.litigations
    def active_litigations(self, *_): return [item for item in self.litigations if item["estado"] == "Activo"]
    def compliance_snapshot(self, *_): return self.compliance
    def incident_rows(self, *_): return self.incidents
    def incident_dimensions(self, *_): return self.known_incidents or [{"area": item["area"], "nivel_severidad": item["nivel_severidad"]} for item in self.incidents]
    def matter_rows(self, *_): return self.matters
    def matter_dimensions(self, *_): return self.known_matters or [{"tipo_asunto": item["tipo_asunto"], "estado": item["estado"]} for item in self.matters]
    def ocr_final_rows(self, *_): return self.ocr


def _request(*codes: str, as_of: date = date(2026, 1, 15)):
    return KpiRecalculationRequest(codes, date(2026, 1, 1), date(2026, 1, 31), as_of)

class _Audit:
    def write_audit_event(self, *_args, **_kwargs): return None

def _service(repository): return KPIRecalculationService(repository, _Audit())
def _context(): return KpiRecalculationContext(uuid4(), uuid4(), process_identifier="test")


def test_li01_always_emits_severities_with_zero_without_imputing_individual_amounts():
    repo = _Repository(); repo.litigations = [{"estado": "Activo", "nivel_severidad": "alto", "estimacion_interna": None, "monto_reclamado": None}]
    result = _service(repo).recalculate(_request("KPI-LI-01"), _context())
    assert [(item.dimensions["nivel_severidad"], item.value, item.availability) for item in result.observations] == [("alto", Decimal(0), "DISPONIBLE"), ("medio", Decimal(0), "DISPONIBLE"), ("bajo", Decimal(0), "DISPONIBLE")]


def test_known_cn03_and_eo01_groupings_without_rows_in_month_emit_zero():
    repo = _Repository()
    repo.known_incidents = [{"area": "Legal", "nivel_severidad": "alto"}]
    repo.known_matters = [{"tipo_asunto": "Contrato", "estado": "Abierto"}]
    result = _service(repo).recalculate(_request("KPI-CN-03", "KPI-EO-01"), _context())
    assert {item.kpi_code: item.value for item in result.observations} == {"KPI-CN-03": Decimal(0), "KPI-EO-01": Decimal(0)}


def test_cd03_uses_percentage_scale_and_unavailable_when_no_final_document():
    repo = _Repository(); repo.ocr = [{"id_documento": str(index), "confianza_agregada": Decimal("0.80") if index < 8 else Decimal("0.79")} for index in range(10)]
    result = _service(repo).recalculate(_request("KPI-CD-03"), _context())
    assert result.observations[0].value == Decimal(80)
    empty = _service(_Repository()).recalculate(_request("KPI-CD-03"), _context()).observations[0]
    assert empty.availability == "NO_DISPONIBLE" and empty.value is None


def test_request_identity_is_stable_per_parent_and_canonical_request():
    parent = uuid4()
    first = _request("KPI-RC-03", "KPI-RC-01")
    assert derive_run_operation_id(parent, first) == derive_run_operation_id(parent, _request("KPI-RC-01", "KPI-RC-03"))
    assert derive_run_operation_id(parent, first) != derive_run_operation_id(parent, _request("KPI-RC-01"))


def test_completed_retry_has_no_new_effect_and_invalid_snapshot_period_is_rejected():
    repo = _Repository(); service = _service(repo); context = _context(); request = _request("KPI-LI-05")
    first = service.recalculate(request, context); second = service.recalculate(request, context)
    assert first.analytic_run_id == second.analytic_run_id and len(repo.runs) == 1
    with pytest.raises(KpiCalculationError):
        service.recalculate(KpiRecalculationRequest(("KPI-RC-03",), date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1)), context)


def test_projection_routing_only_accepts_effective_incorporation():
    def projection(entity: str, required: bool): return ProjectionResult(entity, "id", "INCORPORADO" if required else "IDEMPOTENTE", b"x" * 32, uuid4(), uuid4(), uuid4(), required)
    assert requested_kpis_for_projection((projection("CONTRATO", True), projection("LITIGIO", False))) == ("KPI-RC-01", "KPI-RC-03")
    assert requested_kpis_for_projection((projection("OBLIGACION", True),)) == ("KPI-CN-02", "KPI-CN-03")
