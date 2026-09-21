"""Procesamiento de un candidato documental durable hasta su activación.

Un candidato con texto nativo se fragmenta directamente. Un candidato que
requiere OCR se completa con el pipeline de almacenamiento/OCR antes de
consolidar. La baja confianza es un resultado funcional: el candidato no se
activa y se conserva su registro operativo OCR. La activación es atómica (ADR-005).
"""
from __future__ import annotations

from uuid import UUID

from app.corpus.chunker import ChunkingConfig, chunk_text
from app.documents.models import DocumentVersionState
from app.documents.repository import DocumentsRepository
from app.documents.service import DocumentOperationContext, DocumentsService
from app.ingestion.models import DocumentCandidate, SourceFamily
from app.ocr.confidence import LOW_CONFIDENCE_THRESHOLD, aggregate_confidence
from app.ocr.consolidation import NativePage, consolidate


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
):
    """Procesa un candidato documental y activa la versión + corpus textualmente."""
    repository = DocumentsRepository(conninfo, security.repository)
    service = DocumentsService(repository, security)
    tokenizer = (tokenizer_factory or _default_tokenizer)()

    with repository.transaction() as connection:
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

    # Crear documento + versión candidata.
    with repository.transaction() as connection:
        document = repository.create_or_get_document(
            connection,
            id_documento=id_documento,
            name=str(meta["declared_name"] or id_documento),
            document_type=document_type,
            source_family=family.value,
        )
        version = repository.create_candidate_version(
            connection,
            id_documento=id_documento,
            stored_object_id=meta["stored_object_id"],
            content_sha256=bytes(32),
            operation_id=context.operation_id,
            correlation_id=context.correlation_id,
        )

        # Resultado funcional: baja confianza o texto no utilizable → no se activa.
        if low_confidence or not consolidated.text:
            repository.set_version_state(connection, version.id, DocumentVersionState.RECHAZADA)
            if ocr_used:
                repository.insert_ocr_run(
                    connection,
                    document_version_id=version.id,
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

        chunks = chunk_text(
            consolidated.text,
            tokenizer,
            ChunkingConfig(),
            page_mapping=consolidated.page_map,
        )
        repository.insert_chunks(
            connection,
            version.id,
            [
                {
                    "content": chunk.content,
                    "token_count": chunk.token_count,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "section": chunk.section,
                }
                for chunk in chunks
            ],
        )
        if ocr_used:
            repository.insert_ocr_run(
                connection,
                document_version_id=version.id,
                attempt_number=1,
                estado_ocr=_ocr_estado(aggregate),
                confianza_agregada=aggregate,
                total_page_count=len(candidate.pages),
                processed_page_count=len(required_pages),
                granularity="WORD",
                operation_id=context.operation_id,
                correlation_id=context.correlation_id,
            )
        repository.set_version_state(connection, version.id, DocumentVersionState.LISTA)

    activated = service.activate_candidate(
        id_documento=id_documento,
        candidate_version_id=version.id,
        expected_active_version_id=document.active_version_id,
        context=DocumentOperationContext(
            operation_id=context.operation_id,
            correlation_id=context.correlation_id,
            actor=context.actor,
        ),
    )
    return activated
