"""Runners downstream reales para la coordinación.

Construyen el contexto efectivo con la identidad funcional estable del despacho
(``coordination_dispatch.operation_id``) y delegan en los módulos de dominio.
No reimplementan Validation ni Document.
"""
from __future__ import annotations

from uuid import UUID

from app.coordination.reconstruction import (
    reconstruct_document_candidate,
    reconstruct_family,
    reconstruct_headers,
    reconstruct_records,
)
from app.validation.repository import ValidationRepository
from app.validation.service import ValidationContext, ValidationService


def make_validation_runner(*, conninfo: str, security):
    """Runner que reconstruye el conjunto tabular y ejecuta Validation."""

    def run(context, file_id: UUID) -> UUID | None:
        repository = ValidationRepository(conninfo, security.repository)
        service = ValidationService(repository, security)
        with repository.transaction() as connection:
            family = reconstruct_family(connection, file_id)
            headers = reconstruct_headers(connection, file_id)
            records = reconstruct_records(connection, file_id)
        if family is None or not records:
            return None
        service.validate(
            file_id=file_id,
            family=family,
            headers=headers,
            records=records,
            context=ValidationContext(
                operation_id=context.operation_id,
                correlation_id=context.correlation_id,
                actor=context.actor,
            ),
        )
        return None

    return run


def make_document_runner(*, conninfo: str, security, tokenizer_factory=None, storage_root: str | None = None):
    """Runner que reconstruye el candidato documental y lo procesa.

    El procesamiento documental (versión, fragmentación y activación atómica) se
    delega en el servicio de documentos; la ruta OCR consume el pipeline de
    almacenamiento/OCR cuando el candidato lo requiere.
    """

    def run(context, file_id: UUID) -> UUID | None:
        from app.documents.processing import process_candidate
        from app.ocr.pipeline import make_ocr_pipeline

        with _transaction(conninfo) as connection:
            candidate = reconstruct_document_candidate(connection, file_id)
        if candidate is None:
            return None
        ocr_pipeline = None
        if any(page.requires_ocr for page in candidate.pages) and storage_root:
            ocr_pipeline = make_ocr_pipeline(conninfo=conninfo, storage_root=storage_root)
        return process_candidate(
            conninfo=conninfo,
            security=security,
            file_id=file_id,
            candidate=candidate,
            context=context,
            tokenizer_factory=tokenizer_factory,
            ocr_pipeline=ocr_pipeline,
        )

    return run


def _transaction(conninfo: str):
    import psycopg
    from psycopg.rows import dict_row

    class _Ctx:
        def __enter__(self):
            self._connection = psycopg.connect(conninfo, row_factory=dict_row)
            self._tx = self._connection.transaction()
            self._tx.__enter__()
            return self._connection

        def __exit__(self, *exc):
            try:
                return self._tx.__exit__(*exc)
            finally:
                self._connection.close()

    return _Ctx()
