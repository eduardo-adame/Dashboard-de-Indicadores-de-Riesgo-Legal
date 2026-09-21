"""Pipeline de almacenamiento → rasterización → OCR para páginas escaneadas.

El objeto se obtiene por su localizador opaco a través de la frontera de
almacenamiento, se materializa en un recurso temporal controlado y se elimina al
terminar, tanto en éxito como en error. No se accede a rutas internas de la
ingesta ni a rutas absolutas del host.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from app.ingestion.models import DocumentCandidate
from app.ocr.engine import TesseractOcrEngine
from app.ocr.rasterization import rasterize_pdf
from app.storage import StorageAbstraction


def _locator(conninfo: str, file_id: UUID) -> str | None:
    with psycopg.connect(conninfo, row_factory=dict_row) as connection:
        row = connection.execute(
            """SELECT so.locator
                 FROM app.ingest_file f
                 JOIN app.stored_object so ON so.id = f.stored_object_id
                WHERE f.id = %s""",
            (file_id,),
        ).fetchone()
    return str(row["locator"]) if row else None


def make_ocr_pipeline(*, conninfo: str, storage_root: str, engine=None):
    """Construye un pipeline que reconoce sólo las páginas que requieren OCR."""

    def run(file_id: UUID, candidate: DocumentCandidate) -> dict[int, tuple[str, float | None]]:
        locator = _locator(conninfo, file_id)
        if locator is None:
            raise RuntimeError("objeto almacenado no encontrado")
        storage = StorageAbstraction(Path(storage_root))
        ocr_engine = engine or TesseractOcrEngine(language="spa")
        required = [page.page_number for page in candidate.pages if page.requires_ocr]

        results: dict[int, tuple[str, float | None]] = {}
        # Recurso temporal controlado: se elimina al salir del contexto, en éxito o error.
        with tempfile.TemporaryDirectory(prefix="ocr-") as temporary:
            temp_pdf = Path(temporary) / "document.pdf"
            with storage.open_stream(locator) as stream:
                temp_pdf.write_bytes(stream.read())
            for page_number in required:
                for rasterized in rasterize_pdf(temp_pdf, first_page=page_number, last_page=page_number):
                    page_result = ocr_engine.recognize(rasterized.image, page_number=rasterized.page_number)
                    results[rasterized.page_number] = (page_result.text, page_result.confidence)
        return results

    return run
