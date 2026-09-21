"""Validación estructural de un conjunto tabular ya extraído.

No re-parsea el archivo ni infiere delimitadores: opera sobre la estructura ya
producida por la extracción (encabezados y filas con posición). Comprueba la
presencia y denominación de las columnas obligatorias y la consistencia de la
separación de campos; una inconsistencia estructural rechaza el archivo completo.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.validation.models import DataContract, QuarantineCause, StructuralValidation


def validate_structure(
    headers: Sequence[object],
    rows: Iterable[tuple[int, int]],
    contract: DataContract,
) -> StructuralValidation:
    """Comprueba columnas obligatorias y consistencia de ancho de fila.

    ``rows`` recibe pares ``(row_number, width)`` ya extraídos; sólo se comprueba
    que cada fila conserve el ancho del encabezado.
    """
    normalized_headers = [str(header).strip() for header in headers]

    missing = tuple(column for column in contract.required_columns if column not in normalized_headers)
    if missing:
        return StructuralValidation(
            conforming=False,
            cause=QuarantineCause.STRUCTURAL_INCONSISTENCY,
            missing_columns=missing,
        )

    expected_width = len(normalized_headers)
    inconsistent = tuple(row_number for row_number, width in rows if width != expected_width)
    if inconsistent:
        return StructuralValidation(
            conforming=False,
            cause=QuarantineCause.STRUCTURAL_INCONSISTENCY,
            inconsistent_rows=inconsistent,
        )

    return StructuralValidation(conforming=True, cause=None)
