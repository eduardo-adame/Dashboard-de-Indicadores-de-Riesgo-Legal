"""Tipos del dominio documental."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class DocumentProcessingError(Exception):
    """Error controlado del procesamiento documental."""


class DocumentNotFoundError(DocumentProcessingError):
    """El documento referenciado no existe."""


class VersionNotReadyError(DocumentProcessingError):
    """La versión candidata no está lista para activarse."""


class StaleWriteError(DocumentProcessingError):
    """La versión activa cambió desde que se inició el procesamiento."""


class DocumentVersionState(StrEnum):
    """Estados de versión documental (coinciden con el esquema físico)."""

    PENDIENTE = "PENDIENTE"
    PROCESANDO = "PROCESANDO"
    LISTA = "LISTA"
    RECHAZADA = "RECHAZADA"
    FALLIDA = "FALLIDA"


@dataclass(frozen=True)
class DocumentRef:
    """Identidad estable de un documento lógico."""

    id_documento: str
    name: str
    document_type: str
    source_family: str
    active_version_id: UUID | None = None
    invalidated: bool = False


@dataclass(frozen=True)
class DocumentVersionRef:
    """Versión técnica de un documento (candidata o vigente)."""

    id: UUID
    id_documento: str
    version_number: int
    processing_state: DocumentVersionState
    operation_id: UUID
    correlation_id: UUID
