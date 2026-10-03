"""Procesamiento de un candidato documental durable hasta su activación.

Un candidato con texto nativo se fragmenta directamente. Un candidato que
requiere OCR se completa con el pipeline de almacenamiento/OCR antes de
consolidar. La baja confianza es un resultado funcional: el candidato no se
activa y se conserva su registro operativo OCR. La activación es atómica (ADR-005).
"""
from __future__ import annotations

import hashlib
from uuid import UUID

from app.documents.models import DocumentVersionState, StaleWriteError
from app.documents.repository import DocumentsRepository
from app.documents.service import DocumentOperationContext, DocumentsService
from app.ingestion.models import DocumentCandidate, SourceFamily
from app.ocr.confidence import LOW_CONFIDENCE_THRESHOLD, aggregate_confidence
from app.ocr.consolidation import NativePage, consolidate
from app.rag.index_repository import IndexRepository
from app.rag.indexing import IndexingInvariantError, IndexingService


def _default_tokenizer():
    from app.corpus.tokenizer import BgeM3Tokenizer

    return BgeM3Tokenizer()


def _ocr_estado(aggregate: float | None) -> str:
    if aggregate is None:
        return "Pendiente"
    return "Exitoso" if aggregate >= LOW_CONFIDENCE_THRESHOLD else "Rechazado por baja confianza"


def process_candidate(
    *,
    conninfo: str,
    security,
    file_id: UUID,
    candidate: DocumentCandidate,
    context,
    tokenizer_factory=None,
    ocr_pipeline=None,
    embedding_service=None,
):
    """Procesa, certifica y activa una versión documental de forma recuperable."""
    repository = DocumentsRepository(conninfo, security.repository)
    service = DocumentsService(repository, security)
    index_repository = IndexRepository(conninfo)
    tokenizer = (tokenizer_factory or _default_tokenizer)()
    operation_context = DocumentOperationContext(
        operation_id=context.operation_id,
        correlation_id=context.correlation_id,
        actor=context.actor,
    )

    with repository.transaction() as connection:
        # La autorización vigente precede cualquier lectura o reconciliación de
        # evidencia documental protegida. La activación vuelve a comprobarla.
        service._authorize(connection, operation_context, "document.manage")
        meta = connection.execute(
            "SELECT source_family, exchange_format, declared_name, stored_object_id FROM app.ingest_file WHERE id = %s",
            (file_id,),
        ).fetchone()
    family = SourceFamily(str(meta["source_family"]))
    document_type = str(meta["exchange_format"] or "DOCUMENTO")
    id_documento = str(file_id)

    required_pages = [page.page_number for page in candidate.pages if page.requires_ocr]
    ocr_used = bool(required_pages)
    ocr_text_by_page: dict[int, tuple[str, float | None]] = {}
    if ocr_used:
        if ocr_pipeline is None:
            raise RuntimeError("pipeline OCR no configurado para candidato que requiere OCR")
        ocr_text_by_page = ocr_pipeline(file_id, candidate)

    confidences = [ocr_text_by_page[n][1] for n in required_pages if n in ocr_text_by_page]
    aggregate = aggregate_confidence(confidences) if ocr_used else None
    low_confidence = ocr_used and (aggregate is None or aggregate < LOW_CONFIDENCE_THRESHOLD)

    pages = tuple(NativePage(p.page_number, p.native_text, p.requires_ocr) for p in candidate.pages)
    consolidated = consolidate(pages, ocr_text_by_page)

    # La identidad durable del despacho permite recuperar la misma candidata.
    with repository.transaction() as connection:
        document = repository.create_or_get_document(
            connection,
            id_documento=id_documento,
            name=str(meta["declared_name"] or id_documento),
            document_type=document_type,
            source_family=family.value,
        )
        existing = connection.execute(
            """SELECT id, id_documento, stored_object_id, source_record_id,
                      processing_state, consolidated_text, content_sha256,
                      operation_id, correlation_id
                 FROM app.document_version
                WHERE operation_id = %s FOR UPDATE""",
            (context.operation_id,),
        ).fetchone()
        if existing is None:
            version = repository.create_candidate_version(
                connection,
                id_documento=id_documento,
                stored_object_id=meta["stored_object_id"],
                content_sha256=hashlib.sha256(consolidated.text.encode("utf-8")).digest(),
                operation_id=context.operation_id,
                correlation_id=context.correlation_id,
            )
            version_id = version.id
            version_state = version.processing_state.value
        else:
            if (
                existing["id_documento"] != id_documento
                or existing["stored_object_id"] != meta["stored_object_id"]
                or existing["operation_id"] != context.operation_id
                or existing["correlation_id"] != context.correlation_id
                or (
                    existing["consolidated_text"] is not None
                    and existing["consolidated_text"] != consolidated.text
                )
            ):
                raise IndexingInvariantError(
                    "el retry documental no coincide con la versión persistida"
                )
            version_id = existing["id"]
            version_state = str(existing["processing_state"])

        # Resultado funcional: baja confianza o texto no utilizable → no se activa.
        if low_confidence or not consolidated.text:
            if version_state not in {"RECHAZADA", "FALLIDA"}:
                repository.set_version_state(
                    connection, version_id, DocumentVersionState.RECHAZADA
                )
            if ocr_used and connection.execute(
                "SELECT 1 FROM app.ocr_run WHERE document_version_id = %s",
                (version_id,),
            ).fetchone() is None:
                repository.insert_ocr_run(
                    connection,
                    document_version_id=version_id,
                    attempt_number=1,
                    estado_ocr=_ocr_estado(aggregate),
                    confianza_agregada=aggregate,
                    total_page_count=len(candidate.pages),
                    processed_page_count=len(required_pages),
                    granularity="WORD",
                    operation_id=context.operation_id,
                    correlation_id=context.correlation_id,
                )
            return None

        digest = hashlib.sha256(consolidated.text.encode("utf-8")).digest()
        if version_state in {"RECHAZADA", "FALLIDA"}:
            raise IndexingInvariantError(
                "una versión terminal no puede reutilizarse como candidata de índice"
            )
        if version_state != "LISTA":
            connection.execute(
                """UPDATE app.document_version
                      SET processing_state = 'PROCESANDO',
                          consolidated_text = %s,
                          content_sha256 = %s,
                          completed_at = NULL
                    WHERE id = %s""",
                (consolidated.text, digest, version_id),
            )
        elif (
            existing is not None
            and (
                existing["consolidated_text"] != consolidated.text
                or bytes(existing["content_sha256"]) != digest
            )
        ):
            raise IndexingInvariantError("la versión LISTA no coincide con el retry")

        if ocr_used and connection.execute(
            "SELECT 1 FROM app.ocr_run WHERE document_version_id = %s",
            (version_id,),
        ).fetchone() is None:
            repository.insert_ocr_run(
                connection,
                document_version_id=version_id,
                attempt_number=1,
                estado_ocr=_ocr_estado(aggregate),
                confianza_agregada=aggregate,
                total_page_count=len(candidate.pages),
                processed_page_count=len(required_pages),
                granularity="WORD",
                operation_id=context.operation_id,
                correlation_id=context.correlation_id,
            )

        expected_active_version_id = document.active_version_id

    # BGE-M3 se ejecuta fuera de una transacción PostgreSQL larga.
    indexing = IndexingService(index_repository, encoder=embedding_service)
    indexing.index_version(
        document_version_id=version_id,
        consolidated_text=consolidated.text,
        tokenizer=tokenizer,
        page_mapping=consolidated.page_map,
    )

    # Un retry posterior a la activación revalida el permiso vigente, pero no
    # repite la mutación ni su evento de auditoría exitoso.
    if index_repository.version_is_active(version_id):
        with repository.transaction() as connection:
            service._authorize(connection, operation_context, "document.manage")
        return version_id

    try:
        activated = service.activate_candidate(
            id_documento=id_documento,
            candidate_version_id=version_id,
            expected_active_version_id=expected_active_version_id,
            context=operation_context,
        )
    except StaleWriteError:
        # Dos retries de la misma operación pueden observar el puntero antes de
        # que el otro lo cambie. Si convergieron en esta misma versión, el efecto
        # ya es el solicitado; una candidata distinta continúa siendo obsoleta.
        if index_repository.version_is_active(version_id):
            return version_id
        raise
    return activated
