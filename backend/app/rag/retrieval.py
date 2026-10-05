"""Recuperación híbrida con autorización documental vigente."""
from __future__ import annotations

import psycopg
from psycopg.rows import tuple_row

from app.embeddings import get_embedding_service
from app.rag.context import build_structured_context
from app.rag.evidence import classify_evidence, fuse_candidates
from app.rag.lexical import lexical_term_frequencies
from app.rag.models import RetrievalContext, RetrievalError, RetrievalResult
from app.rag.retrieval_repository import RetrievalRepository, validate_query_vector
from app.security.models import AuthorizationError
from app.security.service import SecurityService


class RetrievalService:
    """Coordina Security y búsquedas con una conexión del llamador en autocommit.

    READ COMMITTED por sentencia evita retener el bloqueo de sesión de Security
    durante la inferencia y permite observar revocaciones antes de entregar texto.
    """

    def __init__(
        self,
        security: SecurityService,
        repository: RetrievalRepository | None = None,
        encoder=None,
    ) -> None:
        self.security = security
        self.repository = repository or RetrievalRepository()
        self.encoder = encoder or get_embedding_service()

    def retrieve(self, connection: psycopg.Connection, context: RetrievalContext) -> RetrievalResult:
        if not isinstance(context, RetrievalContext) or not isinstance(context.query, str):
            raise RetrievalError("Consulta no válida")
        if not context.query.strip():
            raise RetrievalError("Consulta no válida")
        # Cada lectura debe observar las revocaciones confirmadas antes de entregar texto.
        if not connection.autocommit:
            raise RetrievalError("La conexión de recuperación requiere READ COMMITTED sin transacción larga")
        with connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute("SHOW transaction_isolation")
            isolation = str(cursor.fetchone()[0]).lower()
        if isolation != "read committed":
            raise RetrievalError("Aislamiento de recuperación no válido")

        initial_scope = self.security.authorized_document_scope(connection, context.principal)
        try:
            vector = self.encoder.encode_query(context.query)
        except Exception:
            raise RetrievalError("Consulta vectorial no disponible") from None
        vector = validate_query_vector(vector)
        query_terms = tuple(term.normalized_lexeme for term in lexical_term_frequencies(context.query))

        scope = self.security.authorized_document_scope(connection, context.principal)
        if scope != initial_scope:
            raise AuthorizationError("Acceso no autorizado")
        try:
            vector_candidates = self.repository.vector_top10(connection, scope, vector)
            bm25_candidates = self.repository.bm25_top10(connection, scope, query_terms)
        except psycopg.Error:
            raise RetrievalError("La recuperación no pudo completarse") from None

        final_scope = self.security.authorized_document_scope(connection, context.principal)
        if final_scope != scope:
            raise AuthorizationError("Acceso no autorizado")
        candidate_ids = tuple({item.fragment_id for item in (*vector_candidates, *bm25_candidates)})
        try:
            still_authorized = self.repository.final_fragments_still_authorized(
                connection, final_scope, candidate_ids
            )
        except psycopg.Error:
            raise RetrievalError("La recuperación no pudo completarse") from None
        if not still_authorized:
            raise AuthorizationError("Acceso no autorizado")
        fragments = fuse_candidates(vector_candidates, bm25_candidates)
        result = RetrievalResult(
            operation_id=context.operation_id,
            correlation_id=context.correlation_id,
            state=classify_evidence(vector_candidates, bm25_candidates, fragments),
            fragments=fragments,
            structured_context=build_structured_context(fragments),
            context_reference_id=context.context_reference_id,
        )
        delivery_scope = self.security.authorized_document_scope(connection, context.principal)
        if delivery_scope != scope:
            raise AuthorizationError("Acceso no autorizado")
        return result
