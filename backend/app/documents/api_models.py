"""Contratos HTTP operativos, sin texto documental ni claves de almacenamiento."""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OcrQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str | None = Field(default=None, min_length=1, max_length=256)
    state: Literal["Pendiente", "Rechazado por baja confianza"] | None = None
    processed_from: datetime | None = None
    processed_to: datetime | None = None
    limit: int = Field(default=100, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_dates(self):
        for value in (self.processed_from, self.processed_to):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Las fechas requieren zona horaria")
        if self.processed_from and self.processed_to and self.processed_from > self.processed_to:
            raise ValueError("El intervalo de fechas no es válido")
        return self


class OcrItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str
    document_version_id: UUID
    document_name: str
    file_name: str | None
    processing_state: str
    ocr_state: str
    processed_at: datetime | None
    confidence: float | None
    total_page_count: int | None
    ocr_processed_page_count: int | None
    granularity: str | None
    outcome: str


class OcrPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[OcrItem]
    next_cursor: str | None = None


class ReprocessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_document_version_id: UUID
    request_id: UUID


class ReprocessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    document_id: str
    document_version_id: UUID | None
    ocr_state: str
    processing_state: str
    kpi_job_id: UUID | None
    kpi_job_state: str | None
