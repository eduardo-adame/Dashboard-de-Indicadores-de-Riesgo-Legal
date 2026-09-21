"""Política determinista para interpretar CSV admitidos."""
from __future__ import annotations

import csv


DELIMITERS = (",", ";", "\t", "|")


class CsvStructureError(ValueError):
    def __init__(self, cause_code: str) -> None:
        super().__init__(cause_code)
        self.cause_code = cause_code


def determine_delimiter(text: str) -> str | None:
    """Return the sole structural delimiter, or None for one-column CSV."""
    lines = [line for line in text.splitlines() if line]
    if not lines:
        return None

    candidates: list[str] = []
    inconsistent_structure = False
    for delimiter in DELIMITERS:
        try:
            rows = list(csv.reader(lines, delimiter=delimiter, strict=True))
        except csv.Error as exc:
            raise CsvStructureError("UNDETERMINABLE_STRUCTURE") from exc
        widths = [len(row) for row in rows if row]
        if not widths or max(widths) <= 1:
            continue
        if len(set(widths)) == 1:
            candidates.append(delimiter)
        else:
            inconsistent_structure = True

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise CsvStructureError("AMBIGUOUS_DELIMITER")
    if inconsistent_structure:
        raise CsvStructureError("UNDETERMINABLE_STRUCTURE")
    return None
