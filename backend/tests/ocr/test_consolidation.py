"""Pruebas de consolidación de texto nativo y OCR."""
from __future__ import annotations

from app.ocr.consolidation import NativePage, consolidate


def test_native_only_pages_are_consolidated() -> None:
    pages = (NativePage(1, "uno", False), NativePage(2, "dos", False))
    result = consolidate(pages, {})
    assert result.text == "uno\n\ndos"
    assert result.page_map == ((1, 0, 3), (2, 5, 8))


def test_successful_ocr_page_is_included() -> None:
    pages = (NativePage(1, "", True),)
    result = consolidate(pages, {1: ("texto ocr", 0.9)})
    assert result.text == "texto ocr"
    assert result.page_map[0][0] == 1


def test_low_confidence_ocr_page_is_excluded() -> None:
    pages = (NativePage(1, "", True),)
    result = consolidate(pages, {1: ("texto dudoso", 0.5)})
    assert result.text == ""
    assert result.page_map == ()


def test_missing_ocr_page_is_excluded() -> None:
    pages = (NativePage(1, "", True),)
    result = consolidate(pages, {})
    assert result.text == ""


def test_mixed_document_preserves_order_and_mapping() -> None:
    pages = (NativePage(1, "nativa", False), NativePage(2, "", True), NativePage(3, "final", False))
    result = consolidate(pages, {2: ("ocr", 0.95)})
    assert result.text == "nativa\n\nocr\n\nfinal"
    assert [entry[0] for entry in result.page_map] == [1, 2, 3]
