"""Contratos públicos de consulta y paginación de auditoría."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AuditUnavailableError(Exception):
    """No se pudo confirmar una lectura autorizada."""


class AuditQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    action: str | None = Field(default=None, min_length=1, max_length=128)
    resource_type: str | None = Field(default=None, min_length=1, max_length=128)
    correlation_id: UUID | None = None
    limit: int = Field(default=100, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=1024)

    @field_validator("occurred_from", "occurred_to")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("La fecha debe incluir zona horaria")
        return value

    @model_validator(mode="after")
    def valid_range(self):
        if self.occurred_from and self.occurred_to and self.occurred_from > self.occurred_to:
            raise ValueError("El intervalo no es válido")
        return self

    def binding(self, own_account_id: UUID | None) -> str:
        payload = self.model_dump(mode="json", exclude={"cursor", "limit"})
        payload["own_account_id"] = str(own_account_id) if own_account_id else None
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def decode_cursor(self, own_account_id: UUID | None) -> tuple[datetime, UUID] | None:
        if self.cursor is None:
            return None
        try:
            payload = json.loads(base64.b64decode(self.cursor, altchars=b"-_", validate=True))
            if set(payload) != {"at", "id", "binding"} or payload["binding"] != self.binding(own_account_id):
                raise ValueError
            moment, identity = datetime.fromisoformat(payload["at"]), UUID(payload["id"])
            if moment.utcoffset() is None:
                raise ValueError
            return moment, identity
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
            raise ValueError("Cursor no válido para esta consulta") from None

    def encode_cursor(self, moment: datetime, identity: UUID, own_account_id: UUID | None) -> str:
        payload = {"at": moment.isoformat(), "id": str(identity), "binding": self.binding(own_account_id)}
        return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode("utf-8")).decode("ascii")


class AuditResourceResponse(BaseModel):
    resource_type: str
    resource_identifier: str
    id_documento: str | None = None
    fragment_id: UUID | None = None


class AuditEventResponse(BaseModel):
    id: UUID
    occurred_at: datetime
    actor_type: str
    actor_identifier: str
    actor_user_id: UUID | None = None
    action: str
    resource_type: str
    resource_identifier: str | None = None
    result: str
    safe_cause_code: str | None = None
    operation_id: UUID
    correlation_id: UUID
    query_sha256: str | None = None
    resources: list[AuditResourceResponse] = Field(default_factory=list)


class AuditPageResponse(BaseModel):
    items: list[AuditEventResponse]
    next_cursor: str | None = None
