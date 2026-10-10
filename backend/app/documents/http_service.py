"""Frontera administrativa HTTP que delega el reproceso al pipeline existente."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
from uuid import UUID, uuid4, uuid5

import psycopg

from app.coordination.kpi_integration import KpiIntegrationError
from app.coordination.service import DispatchContext
from app.documents.api_models import OcrItem, OcrPage, OcrQuery, ReprocessResponse
from app.documents.models import DocumentProcessingError
from app.rag.indexing import IndexingError
from app.security.models import AuditPersistenceError, SecurityError


_NAMESPACE = UUID("cb07bab5-e56c-44e0-a2f9-d910659c9d91")


class DocumentHttpConflict(DocumentProcessingError):
    """La fuente o el intento no permiten una operación administrativa segura."""


class DocumentHttpUnavailable(DocumentProcessingError):
    """No se confirmó el procesamiento solicitado."""


class UpstreamCommittedKpiRetryable(DocumentHttpUnavailable):
    def __init__(self, operation_id):
        self.operation_id = operation_id
        super().__init__("UPSTREAM_COMMITTED_KPI_RETRYABLE")


def reprocess_operation_id(account_id, document_id, source_version_id, request_id):
    identity = json.dumps([str(account_id), document_id, str(source_version_id), str(request_id)],
                          ensure_ascii=False, separators=(",", ":"))
    return uuid5(_NAMESPACE, "document-ocr-reprocess:v1:" + identity)


def _filter_hash(query):
    raw = query.model_dump(mode="json", exclude={"cursor", "limit"})
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


def _cursor(row, query):
    value = [row["created_at"].isoformat(), str(row["document_version_id"]), _filter_hash(query)]
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def _decode_cursor(query):
    if query.cursor is None:
        return None
    try:
        value = json.loads(base64.b64decode(query.cursor + "=" * (-len(query.cursor) % 4),
                                           altchars=b"-_", validate=True))
        if not isinstance(value, list) or len(value) != 3 or value[2] != _filter_hash(query):
            raise ValueError
        stamp = datetime.fromisoformat(value[0])
        if stamp.utcoffset() is None:
            raise ValueError
        return stamp, UUID(value[1])
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Cursor no válido") from None


def _reprocess_eligible(source):
    """La consulta y la acción comparten las precondiciones de procedencia OCR."""
    return bool(source is not None and (
        source["source_verified"]
        and source["invalidated_at"] is None
        and source["file_state"] == "COMPLETADO"
        and source["technical_result"] == "ACCEPTED"
        and not source["quarantined"]
        and source["requires_ocr"]
        and source["processing_state"] != "FALLIDA"
        and source["ocr_state"] in (None, "Pendiente", "Rechazado por baja confianza")
    ))


class DocumentHttpService:
    def __init__(self, repository, security, *, runner=None, kpi_integration=None):
        self.repository = repository
        self.security = security
        self.runner = runner
        self.kpi_integration = kpi_integration

    def _authorize(self, connection, principal):
        return self.security.revalidate_functional_access(connection, principal, "document.manage")

    def _denial(self, principal, correlation_id):
        # La excepción se devuelve después de confirmar el único rechazo auditable.
        with self.repository.transaction() as connection:
            self.repository.write_audit_event(
                connection, actor=principal, action="AUTHORIZATION_DENIED",
                resource_type="DOCUMENT", resource_identifier=None, result="DENIED",
                correlation_id=correlation_id, safe_cause_code="AUTHORIZATION_DENIED",
            )

    def list_ocr(self, query: OcrQuery, principal) -> OcrPage:
        try:
            with self.repository.transaction() as connection:
                current = self._authorize(connection, principal)
                rows = self.repository.list_ocr_operations(connection, query=query,
                                                           after=_decode_cursor(query))
                page_rows = rows[:query.limit]
                items = [OcrItem(**{**{key: row[key] for key in OcrItem.model_fields
                                      if key not in {"outcome", "ocr_applicable", "reprocess_eligible"}},
                                    "outcome": row["ocr_state"],
                                    "ocr_applicable": row["requires_ocr"],
                                    "reprocess_eligible": _reprocess_eligible(row)}) for row in page_rows]
                self._authorize(connection, current)
                return OcrPage(items=items, next_cursor=(
                    _cursor(page_rows[-1], query) if len(rows) > query.limit else None))
        except AuditPersistenceError:
            raise
        except SecurityError:
            self._denial(principal, uuid4())
            raise

    def reprocess(self, document_id, payload, principal) -> ReprocessResponse:
        correlation_id = uuid4()
        try:
            # No se consulta procedencia ni se toma un lock documental antes de autorizar.
            with self.repository.transaction() as connection:
                current = self._authorize(connection, principal)
            operation_id = reprocess_operation_id(current.account_id, document_id,
                                                 payload.source_document_version_id, payload.request_id)
            # El lock de sesión coordina el intento lógico; OCR/BGE no mantienen
            # transacciones largas ni locks de cuenta mientras hacen inferencia.
            with psycopg.connect(self.repository._conninfo, autocommit=True) as coordinator:
                key = "document-http:" + str(operation_id)
                coordinator.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
                try:
                    with self.repository.transaction() as connection:
                        current = self._authorize(connection, principal)
                        source = self.repository.ocr_reprocess_source(
                            connection, document_id, payload.source_document_version_id)
                        if not _reprocess_eligible(source):
                            raise DocumentHttpConflict("Fuente no elegible para reproceso")
                        existing = self.repository.ocr_operation_result(connection, operation_id)
                        if existing is not None:
                            if (existing["document_id"] != document_id
                                    or existing["stored_object_id"] != source["stored_object_id"]):
                                raise DocumentHttpConflict("Identidad de intento incompatible")
                            correlation_id = existing["correlation_id"]
                    context = DispatchContext(operation_id, correlation_id, current)
                    if self.kpi_integration is None or self.runner is None:
                        raise DocumentHttpUnavailable("Procesamiento no configurado")
                    if existing is not None and existing["processing_state"] == "FALLIDA":
                        raise DocumentHttpUnavailable("Intento fallido no reutilizable")
                    if existing is not None and existing["processing_state"] in ("LISTA", "RECHAZADA"):
                        outcome = self.kpi_integration.resume(operation_id=operation_id)
                        if outcome is None:
                            self.kpi_integration.process_final_ocr(
                                operation_id=operation_id, correlation_id=correlation_id)
                    else:
                        self.runner(context, source["ingest_file_id"])
                    with self.repository.transaction() as connection:
                        self._authorize(connection, current)
                        result = self.repository.ocr_operation_result(connection, operation_id)
                        if result is None or result["document_id"] != document_id:
                            raise DocumentHttpUnavailable("Resultado no confirmado")
                        return ReprocessResponse(operation_id=operation_id, **{
                            field: result[field] for field in ReprocessResponse.model_fields
                            if field != "operation_id"})
                finally:
                    coordinator.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
        except AuditPersistenceError:
            raise
        except SecurityError:
            self._denial(principal, correlation_id)
            raise
        except KpiIntegrationError:
            raise UpstreamCommittedKpiRetryable(operation_id) from None
        except IndexingError:
            raise DocumentHttpConflict("Evidencia documental incompatible") from None
        except (OSError, RuntimeError):
            raise DocumentHttpUnavailable("Procesamiento no confirmado") from None
