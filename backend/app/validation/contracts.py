"""Contratos de datos por familia del MVP.

Las columnas, catálogos y reglas provienen del contrato de datos del SRS; no se
inventan campos ni catálogos adicionales.
"""
from __future__ import annotations

from app.ingestion.models import SourceFamily
from app.validation.models import DataContract, FieldSpec, FieldType

_REVISION_CATALOG = frozenset(
    {"No iniciado", "En revisión", "En renovación", "Renovado", "Cancelado"}
)
_SEVERITY_CATALOG = frozenset({"alto", "medio", "bajo"})
_LEGAL_MATTER_TYPE_CATALOG = frozenset({"Contrato", "Litigio", "Cumplimiento", "Auditoría"})


AUDIT_INCIDENT = DataContract(
    family=SourceFamily.INTERNAL_AUDIT,
    stable_id="ID_Incidente",
    required_columns=("ID_Incidente", "Fecha_Evento", "Area", "Nivel_Severidad"),
    fields=(
        FieldSpec("ID_Incidente", FieldType.TEXT, required=True),
        FieldSpec("Fecha_Evento", FieldType.DATE, required=True),
        FieldSpec("Area", FieldType.TEXT, required=True),
        FieldSpec("Nivel_Severidad", FieldType.CATALOG, required=True, catalog=_SEVERITY_CATALOG),
    ),
)


AUDIT_LEGAL_MATTER = DataContract(
    family=SourceFamily.INTERNAL_AUDIT,
    stable_id="ID_Asunto",
    required_columns=("ID_Asunto", "Tipo_Asunto", "Estado", "Fecha"),
    fields=(
        FieldSpec("ID_Asunto", FieldType.TEXT, required=True),
        FieldSpec("Tipo_Asunto", FieldType.CATALOG, required=True, catalog=_LEGAL_MATTER_TYPE_CATALOG),
        FieldSpec("Estado", FieldType.TEXT, required=True),
        FieldSpec("Fecha", FieldType.DATE, required=True),
    ),
)

CONTRACTS: dict[SourceFamily, DataContract] = {
    SourceFamily.CONTRACTS_DOCUMENTS: DataContract(
        family=SourceFamily.CONTRACTS_DOCUMENTS,
        stable_id="ID_Contrato",
        required_columns=("ID_Contrato", "Fecha_Solicitud", "Fecha_Vencimiento", "Estado_Revision"),
        fields=(
            FieldSpec("ID_Contrato", FieldType.TEXT, required=True),
            FieldSpec("Fecha_Solicitud", FieldType.DATE, required=True),
            FieldSpec("Fecha_Firma", FieldType.DATE, required=False, date_not_before="Fecha_Solicitud"),
            FieldSpec("Fecha_Vencimiento", FieldType.DATE, required=True),
            FieldSpec("Estado_Revision", FieldType.CATALOG, required=True, catalog=_REVISION_CATALOG),
        ),
    ),
    SourceFamily.LITIGATION: DataContract(
        family=SourceFamily.LITIGATION,
        stable_id="ID_Litigio",
        required_columns=("ID_Litigio", "Fecha_Apertura", "Estado", "Nivel_Severidad"),
        fields=(
            FieldSpec("ID_Litigio", FieldType.TEXT, required=True),
            FieldSpec("Fecha_Apertura", FieldType.DATE, required=True),
            FieldSpec("Estado", FieldType.TEXT, required=True),
            FieldSpec("Nivel_Severidad", FieldType.CATALOG, required=True, catalog=_SEVERITY_CATALOG),
            FieldSpec("Monto_Reclamado", FieldType.DECIMAL_NON_NEGATIVE, required=False),
            FieldSpec("Estimacion_Interna", FieldType.DECIMAL_NON_NEGATIVE, required=False),
        ),
    ),
    SourceFamily.COMPLIANCE: DataContract(
        family=SourceFamily.COMPLIANCE,
        stable_id="ID_Obligacion",
        required_columns=("ID_Obligacion", "Fecha_Limite"),
        fields=(
            FieldSpec("ID_Obligacion", FieldType.TEXT, required=True),
            FieldSpec("Fecha_Limite", FieldType.DATE, required=True),
            FieldSpec("Evidencia_Cumplimiento", FieldType.TEXT, required=False),
        ),
    ),
    SourceFamily.INTERNAL_AUDIT: AUDIT_INCIDENT,
}

_COMPOSITE_CONTRACTS: dict[SourceFamily, tuple[DataContract, ...]] = {
    SourceFamily.INTERNAL_AUDIT: (AUDIT_INCIDENT, AUDIT_LEGAL_MATTER),
}


def contract_for_family(family: SourceFamily) -> DataContract:
    """Devuelve el contrato de datos de una familia del MVP."""
    try:
        return CONTRACTS[family]
    except KeyError:  # pragma: no cover - defensivo
        raise ValueError(f"familia sin contrato de datos: {family}") from None


def contracts_for_family(family: SourceFamily) -> tuple[DataContract, ...]:
    """Devuelve los contratos aplicables a una familia, en orden determinista."""
    try:
        return _COMPOSITE_CONTRACTS.get(family, (CONTRACTS[family],))
    except KeyError:  # pragma: no cover - defensivo
        raise ValueError(f"familia sin contrato de datos: {family}") from None
