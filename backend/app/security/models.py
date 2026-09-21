"""Tipos de dominio para autenticación y autorización."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class SecurityError(Exception):
    """Error de seguridad que puede convertirse en una respuesta segura."""


class AuthenticationError(SecurityError):
    """La identidad o sesión no puede autenticarse."""


class AuthorizationError(SecurityError):
    """La operación no está autorizada."""


class ValidationError(SecurityError):
    """La entrada no cumple una precondición de seguridad."""


class AuditPersistenceError(SecurityError):
    """Una operación auditable no pudo confirmar su evento."""


class BootstrapError(SecurityError):
    """El aprovisionamiento inicial no puede continuar de forma segura."""


class BootstrapAlreadyCompletedError(BootstrapError):
    """Ya existe una identidad TI activa que administra normalmente el sistema."""


class DocumentAuthorizationResult(StrEnum):
    PERMIT = "PERMIT"
    EXPLICIT_DENY = "EXPLICIT_DENY"
    DEFAULT_DENY = "DEFAULT_DENY"


ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "JURIDICO": frozenset({"dashboard.read", "kpi.read", "document.query"}),
    "ANALISTA": frozenset({
        "dashboard.read", "kpi.read", "document.query", "ingest.upload",
        "ingest.execute", "quarantine.read", "quarantine.reinject",
        "quarantine.discard", "audit.read.own", "document.manage",
    }),
    "TI": frozenset({
        "dashboard.read", "kpi.read", "document.query", "ingest.upload",
        "ingest.execute", "quarantine.read", "quarantine.reinject",
        "quarantine.discard", "audit.read.own", "audit.read.all", "user.create",
        "user.update", "user.disable", "user.reset_access", "role.assign",
        "document_acl.manage", "technical_config.manage", "document.manage",
    }),
}

KNOWN_ROLES = frozenset(ROLE_PERMISSIONS)
KNOWN_PERMISSIONS = frozenset().union(*ROLE_PERMISSIONS.values())
KNOWN_FAMILIES = frozenset({
    "CONTRATOS_DOCUMENTOS", "LITIGIOS", "CUMPLIMIENTO", "AUDITORIA_INTERNA",
})


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Identidad autenticada evaluada contra el estado vigente."""

    account_id: UUID
    session_id: UUID
    username: str
    authorization_version: int
    roles: frozenset[str]
    permissions: frozenset[str]


@dataclass(frozen=True)
class FunctionalAuthorizationDecision:
    """Resultado de evaluar una capacidad funcional."""

    permitted: bool
    capability: str
    authorization_version: int
    safe_reason: str


@dataclass(frozen=True)
class AuthorizationContext:
    """Contexto vigente para operaciones funcionales y documentales."""

    principal: AuthenticatedPrincipal
    correlation_id: UUID


@dataclass(frozen=True)
class AuthorizedDocumentScope:
    """Ámbito que debe aplicarse antes de consultar documentos o corpus."""

    account_id: UUID
    role_ids: frozenset[str]
    allowed_families: frozenset[str]

    def includes_family(self, source_family: str) -> bool:
        """Indica si el grant de familia permite acceso potencial."""
        return source_family in self.allowed_families

    def database_predicate(self, document_alias: str = "document") -> tuple[str, dict[str, object]]:
        """Devuelve un predicado componible para aplicar antes de retrieval.

        El consumidor añade este predicado a su consulta de documentos; las
        excepciones explícitas conservan precedencia sobre el grant por familia.
        """
        if not document_alias.replace("_", "").isalnum():
            raise ValueError("El alias del documento debe ser un identificador SQL simple")
        return (
            f"""
            NOT EXISTS (
              SELECT 1 FROM app.document_exception deny_rule
              WHERE deny_rule.active
                AND deny_rule.decision = 'DENY'
                AND deny_rule.id_documento = {document_alias}.id_documento
                AND (deny_rule.user_id = %(scope_user_id)s
                     OR deny_rule.role_id = ANY(%(scope_role_ids)s))
            )
            AND (
              EXISTS (
                SELECT 1 FROM app.document_exception allow_rule
                WHERE allow_rule.active
                  AND allow_rule.decision = 'ALLOW'
                  AND allow_rule.id_documento = {document_alias}.id_documento
                  AND (allow_rule.user_id = %(scope_user_id)s
                       OR allow_rule.role_id = ANY(%(scope_role_ids)s))
              )
              OR {document_alias}.source_family = ANY(%(scope_families)s)
            )
            """.strip(),
            {
                "scope_user_id": self.account_id,
                "scope_role_ids": sorted(self.role_ids),
                "scope_families": sorted(self.allowed_families),
            },
        )


@dataclass(frozen=True)
class DocumentAuthorizationDecision:
    """Decisión sobre un documento individual usando ACL vigente."""

    result: DocumentAuthorizationResult
    scope: AuthorizedDocumentScope
    authorization_version: int

    @property
    def permitted(self) -> bool:
        return self.result is DocumentAuthorizationResult.PERMIT
