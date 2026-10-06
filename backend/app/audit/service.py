"""Consulta bajo autorización vigente y separación física de identidades."""
from uuid import uuid4

from app.audit.models import AuditQuery, AuditUnavailableError
from app.security.models import SecurityError


class AuditQueryService:
    def __init__(self, repository, security):
        self.repository, self.security = repository, security

    def list_events(self, principal, query: AuditQuery):
        denied, result = None, None
        try:
            with self.security.repository.transaction() as connection:
                try:
                    current = self.security.revalidate_functional_access(connection, principal, "audit.read.own")
                    full_access = "audit.read.all" in current.permissions
                    if full_access:
                        current = self.security.revalidate_functional_access(connection, current, "audit.read.all")
                    own_account_id = None if full_access else current.account_id
                    query.decode_cursor(own_account_id)
                    # Los locks de sesión permanecen durante la lectura y el filtrado de respuesta.
                    with self.repository.read_transaction() as reader:
                        result = self.repository.list_events(reader, query, own_account_id)
                    self.security.revalidate_functional_access(connection, current, "audit.read.all" if full_access else "audit.read.own")
                except SecurityError as exc:
                    denied = exc
                    self.security.repository.write_audit_event(
                        connection, actor=principal, action="AUTHORIZATION_DENIED", resource_type="AUDIT",
                        resource_identifier=None, result="DENIED", correlation_id=uuid4(),
                        safe_cause_code="AUTHORIZATION_DENIED",
                    )
        except SecurityError:
            raise AuditUnavailableError("Consulta de auditoría no disponible") from None
        if denied is not None:
            raise denied
        return result
