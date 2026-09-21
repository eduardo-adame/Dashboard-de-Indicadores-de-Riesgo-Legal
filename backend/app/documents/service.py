"""Casos de uso del ciclo de vida documental.

La activación de una versión candidata y la coherencia de su corpus constituyen
una única unidad lógica: si la activación, la coherencia del corpus o la
auditoría obligatoria fallan, la versión anterior permanece activa.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.documents.repository import DocumentsRepository
from app.security.models import AuthenticatedPrincipal, SecurityError


@dataclass(frozen=True)
class DocumentOperationContext:
    operation_id: UUID
    correlation_id: UUID
    actor: AuthenticatedPrincipal


# Capacidad funcional de gestión/reproceso documental. Restringida a Analista y
# TI; la lectura documental (`document.query`) permanece separada y no autoriza
# operaciones administrativas.
MANAGEMENT_CAPABILITY = "document.manage"


class DocumentsService:
    def __init__(self, repository: DocumentsRepository, security) -> None:
        self.repository = repository
        self.security = security

    def activate_candidate(
        self,
        *,
        id_documento: str,
        candidate_version_id: UUID,
        expected_active_version_id: UUID | None,
        context: DocumentOperationContext,
        capability: str = MANAGEMENT_CAPABILITY,
    ) -> UUID:
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            document = self.repository.activate_processed_candidate(
                connection,
                id_documento=id_documento,
                candidate_version_id=candidate_version_id,
                expected_active_version_id=expected_active_version_id,
                actor=context.actor,
                operation_id=context.operation_id,
                correlation_id=context.correlation_id,
            )
            self.repository.write_audit_event(
                connection,
                actor=context.actor,
                action="DOCUMENT_VERSION_ACTIVATED",
                resource_type="DOCUMENT",
                resource_identifier=id_documento,
                result="SUCCESS",
                correlation_id=context.correlation_id,
            )
            return document.active_version_id  # type: ignore[return-value]

    def invalidate(
        self,
        *,
        id_documento: str,
        context: DocumentOperationContext,
        capability: str = MANAGEMENT_CAPABILITY,
    ) -> None:
        with self.repository.transaction() as connection:
            self._authorize(connection, context, capability)
            self.repository.invalidate_document(connection, id_documento)
            self.repository.write_audit_event(
                connection,
                actor=context.actor,
                action="DOCUMENT_INVALIDATED",
                resource_type="DOCUMENT",
                resource_identifier=id_documento,
                result="SUCCESS",
                correlation_id=context.correlation_id,
            )

    def _authorize(self, connection, context: DocumentOperationContext, capability: str) -> None:
        try:
            self.security.revalidate_functional_access(connection, context.actor, capability)
        except SecurityError as exc:
            self.repository.write_audit_event(
                connection,
                actor=context.actor,
                action="DOCUMENT_DENIED",
                resource_type="DOCUMENT",
                resource_identifier=None,
                result="DENIED",
                correlation_id=context.correlation_id,
                safe_cause_code=exc.__class__.__name__.upper(),
            )
            raise
