"""Servicios de autenticación y autorización del backend."""

from app.security.models import (
    AuthenticatedPrincipal,
    AuthorizationContext,
    AuthorizedDocumentScope,
    BootstrapAlreadyCompletedError,
    BootstrapError,
    DocumentAuthorizationDecision,
    FunctionalAuthorizationDecision,
)
from app.security.service import SecurityService

__all__ = (
    "AuthenticatedPrincipal",
    "AuthorizationContext",
    "AuthorizedDocumentScope",
    "BootstrapAlreadyCompletedError",
    "BootstrapError",
    "DocumentAuthorizationDecision",
    "FunctionalAuthorizationDecision",
    "SecurityService",
)
