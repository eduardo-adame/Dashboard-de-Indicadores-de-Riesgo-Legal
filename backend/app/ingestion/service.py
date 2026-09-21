"""Casos de uso de recepción, extracción y bifurcación contractual."""
from __future__ import annotations

import csv
import hashlib
import mimetypes
import os
from pathlib import Path
import stat
from typing import BinaryIO
from uuid import UUID, uuid4
import zipfile
from xml.etree.ElementTree import ParseError

from docx.opc.exceptions import PackageNotFoundError
from lxml.etree import XMLSyntaxError
from openpyxl.utils.exceptions import InvalidFileException
from pypdf.errors import PdfReadError

from app.ingestion.detection import detect_format
from app.ingestion.document_candidate_repository import DocumentCandidateRepository
from app.ingestion.extraction import extract_document, extract_tabular
from app.ingestion.models import (
    CONTROLLED_LOCATIONS,
    ExchangeFormat,
    RoutingTarget,
    IdempotencyConflictError,
    IngestionLimits,
    IngestionResult,
    RejectedFileError,
    ResourceLimitExceeded,
    SourceFamily,
    StagedFile,
)
from app.ingestion.repository import IngestionRepository
from app.security.models import AuthenticatedPrincipal, SecurityError
from app.security.service import SecurityService


class IngestionService:
    def __init__(self, repository: IngestionRepository, security: SecurityService, storage_root: Path, limits: IngestionLimits) -> None:
        self.repository = repository
        self.security = security
        self.storage_root = storage_root
        self.limits = limits

    def _stage(self, stream: BinaryIO, original_name: str) -> StagedFile:
        safe_name = Path(original_name).name
        if not safe_name or safe_name != original_name:
            raise RejectedFileError("INVALID_FILE_NAME")
        temporary_root = self.storage_root / "staging"
        temporary_root.mkdir(parents=True, exist_ok=True)
        temporary = temporary_root / f"{uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as target:
                while True:
                    remaining = self.limits.max_file_bytes - size
                    chunk = stream.read(min(self.limits.stream_chunk_bytes, remaining + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.limits.max_file_bytes:
                        raise ResourceLimitExceeded(size, safe_name)
                    digest.update(chunk)
                    target.write(chunk)
            locator = f"objects/{digest.hexdigest()[:2]}/{uuid4().hex}-{safe_name}"
            destination = self.storage_root / Path(locator)
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(destination)
            return StagedFile(destination, locator, size, digest.digest(), safe_name)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _existing_result(row: dict[str, object]) -> IngestionResult:
        exchange_format = ExchangeFormat(str(row["exchange_format"])) if row["exchange_format"] else None
        state = str(row["state"])
        if state == "CUARENTENA":
            routing_target = RoutingTarget.VALIDATION
        elif state == "COMPLETADO" and exchange_format in {ExchangeFormat.CSV, ExchangeFormat.XLSX}:
            routing_target = RoutingTarget.VALIDATION
        elif state == "COMPLETADO" and exchange_format in {ExchangeFormat.PDF, ExchangeFormat.DOCX}:
            routing_target = RoutingTarget.DOCUMENT
        else:
            routing_target = RoutingTarget.NONE
        return IngestionResult(
            file_id=row["id"], operation_id=row["operation_id"], correlation_id=row["correlation_id"],
            state=state, format=exchange_format.value if exchange_format else None,
            family=SourceFamily(str(row["source_family"])), routing_target=routing_target,
            safe_cause_code=str(row["safe_cause_code"]) if row["safe_cause_code"] else None,
            idempotent=True,
        )

    def ingest_stream(
        self,
        stream: BinaryIO,
        *,
        original_name: str,
        controlled_location: str,
        principal: AuthenticatedPrincipal,
        capability: str,
        source_locator: str,
        idempotency_key: str | None,
        correlation_id: UUID | None = None,
    ) -> IngestionResult:
        if controlled_location not in CONTROLLED_LOCATIONS:
            raise RejectedFileError("UNKNOWN_CONTROLLED_LOCATION")
        family = CONTROLLED_LOCATIONS[controlled_location]
        operation_id = uuid4()
        correlation = correlation_id or uuid4()
        file_id = uuid4()
        try:
            staged = self._stage(stream, original_name)
        except ResourceLimitExceeded as limit:
            return self._persist_resource_limit_rejection(
                limit=limit, family=family, principal=principal, capability=capability,
                source_locator=source_locator, idempotency_key=idempotency_key, operation_id=operation_id,
                correlation_id=correlation, file_id=file_id,
            )
        try:
            detection = detect_format(staged.path, original_name, self.limits)
        except Exception:
            staged.path.unlink(missing_ok=True)
            raise
        records = None
        document = None
        routing_target = RoutingTarget.NONE
        state = "RECHAZADO"
        technical_result = "REJECTED"
        cause = detection.safe_cause_code
        if detection.supported and not detection.accepted:
            state = "CUARENTENA"
            technical_result = "FAILED"
            routing_target = RoutingTarget.VALIDATION
        if detection.accepted and detection.exchange_format:
            try:
                if detection.exchange_format in {ExchangeFormat.CSV, ExchangeFormat.XLSX}:
                    records = extract_tabular(staged.path, detection.exchange_format, self.limits)
                    routing_target = RoutingTarget.VALIDATION
                else:
                    document = extract_document(staged.path, detection.exchange_format)
                    routing_target = RoutingTarget.DOCUMENT
                state = "COMPLETADO"
                technical_result = "ACCEPTED"
                cause = None
            except RejectedFileError as exc:
                state = "CUARENTENA"
                technical_result = "FAILED"
                cause = exc.cause_code
                routing_target = RoutingTarget.VALIDATION
            except (
                csv.Error,
                InvalidFileException,
                KeyError,
                OSError,
                PackageNotFoundError,
                ParseError,
                PdfReadError,
                UnicodeError,
                XMLSyntaxError,
                zipfile.BadZipFile,
            ):
                state = "CUARENTENA"
                technical_result = "FAILED"
                cause = "TECHNICAL_READ_FAILURE"
                routing_target = RoutingTarget.VALIDATION
            except Exception:
                staged.path.unlink(missing_ok=True)
                raise

        object_id = uuid4()
        denied: SecurityError | None = None
        try:
            with self.repository.transaction() as connection:
                try:
                    current = self.security.revalidate_functional_access(connection, principal, capability)
                except SecurityError as exc:
                    self.security.repository.write_audit_event(
                        connection, actor=principal, action="INGESTION_DENIED", resource_type="INGESTION",
                        resource_identifier=None, result="DENIED", correlation_id=correlation,
                        safe_cause_code=exc.__class__.__name__.upper(),
                    )
                    denied = exc
                    current = None
                if denied is None and current is not None:
                    self.repository.lock_source(connection, source_locator)
                    existing, conflict = self.repository.find_existing(
                        connection, source_locator=source_locator, actor_identifier=current.username,
                        idempotency_key=idempotency_key, content_sha256=staged.sha256,
                    )
                    if conflict:
                        raise IdempotencyConflictError("La clave ya corresponde a otro contenido")
                    if existing is not None:
                        staged.path.unlink(missing_ok=True)
                        return self._existing_result(existing)
                    revision = self.repository.next_revision(connection, source_locator)
                    self.repository.insert_file(
                        connection, file_id=file_id, object_id=object_id, operation_id=operation_id,
                        correlation_id=correlation, staged=staged, source_locator=source_locator,
                        source_revision=revision, actor_identifier=current.username,
                        idempotency_key=idempotency_key, family=family.value,
                        detection=detection, state=state, technical_result=technical_result,
                        mime_type=mimetypes.guess_type(original_name)[0] or "application/octet-stream",
                        safe_cause_code=cause,
                    )
                    if records is not None:
                        self.repository.insert_rows(connection, file_id, records)
                    if document is not None:
                        DocumentCandidateRepository.persist(connection, file_id=file_id, candidate=document)
                    if state == "CUARENTENA":
                        quarantine_id = self.repository.insert_quarantine(
                            connection, file_id=file_id, object_id=object_id, cause_code=cause or "TECHNICAL_FAILURE",
                            operation_id=uuid4(), correlation_id=correlation,
                        )
                        self.security.repository.write_audit_event(
                            connection, actor=current, action="QUARANTINE_CREATED", resource_type="QUARANTINE_ITEM",
                            resource_identifier=str(quarantine_id), result="SUCCESS", correlation_id=correlation,
                            safe_cause_code=cause,
                        )
                    self.security.repository.write_audit_event(
                        connection, actor=current, action="INGESTION_COMPLETED", resource_type="INGEST_FILE",
                        resource_identifier=str(file_id), result=technical_result, correlation_id=correlation,
                        safe_cause_code=cause,
                    )
        except Exception:
            if staged.path.exists() and denied is None:
                staged.path.unlink(missing_ok=True)
            raise
        if denied is not None:
            staged.path.unlink(missing_ok=True)
            raise denied
        return IngestionResult(
            file_id, operation_id, correlation, state,
            detection.exchange_format.value if detection.exchange_format else None,
            family, routing_target, cause, False, records, document,
        )

    def run_location(self, controlled_root: Path, controlled_location: str, principal: AuthenticatedPrincipal) -> list[IngestionResult]:
        if controlled_location not in CONTROLLED_LOCATIONS:
            raise RejectedFileError("UNKNOWN_CONTROLLED_LOCATION")
        location = controlled_root / controlled_location
        if not location.exists() or location.is_symlink():
            return []
        root_resolved = controlled_root.resolve(strict=True)
        location_resolved = location.resolve(strict=True)
        if not location_resolved.is_relative_to(root_resolved):
            return []
        results: list[IngestionResult] = []
        for path in sorted(location.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_relative_to(location_resolved):
                continue
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    continue
                with os.fdopen(descriptor, "rb") as stream:
                    descriptor = -1
                    results.append(
                        self.ingest_stream(
                            stream, original_name=path.name, controlled_location=controlled_location,
                            principal=principal, capability="ingest.execute",
                            source_locator=f"controlled/{controlled_location}/{path.name}", idempotency_key=None,
                        )
                    )
            finally:
                if descriptor != -1:
                    os.close(descriptor)
        return results

    def _persist_resource_limit_rejection(
        self,
        *,
        limit: ResourceLimitExceeded,
        family: SourceFamily,
        principal: AuthenticatedPrincipal,
        capability: str,
        source_locator: str,
        idempotency_key: str | None,
        operation_id: UUID,
        correlation_id: UUID,
        file_id: UUID,
    ) -> IngestionResult:
        denied: SecurityError | None = None
        with self.repository.transaction() as connection:
            try:
                current = self.security.revalidate_functional_access(connection, principal, capability)
            except SecurityError as exc:
                self.security.repository.write_audit_event(
                    connection, actor=principal, action="INGESTION_DENIED", resource_type="INGESTION",
                    resource_identifier=None, result="DENIED", correlation_id=correlation_id,
                    safe_cause_code=exc.__class__.__name__.upper(),
                )
                denied = exc
                current = None
            if denied is None and current is not None:
                self.repository.lock_source(connection, source_locator)
                revision = self.repository.next_revision(connection, source_locator)
                self.repository.insert_resource_limit_rejection(
                    connection, file_id=file_id, operation_id=operation_id, correlation_id=correlation_id,
                    original_name=limit.original_name, source_locator=source_locator, source_revision=revision,
                    actor_identifier=current.username, idempotency_key=idempotency_key, family=family.value,
                    observed_byte_size=limit.observed_byte_size,
                )
                self.security.repository.write_audit_event(
                    connection, actor=current, action="INGESTION_COMPLETED", resource_type="INGEST_FILE",
                    resource_identifier=str(file_id), result="REJECTED", correlation_id=correlation_id,
                    safe_cause_code=limit.cause_code,
                )
        if denied is not None:
            raise denied
        return IngestionResult(
            file_id, operation_id, correlation_id, "RECHAZADO", None, family, RoutingTarget.NONE,
            limit.cause_code,
        )
