"""Comando local de una sola ejecución para la primera identidad TI."""
from __future__ import annotations

import sys

from app.config import get_settings
from app.security.models import BootstrapAlreadyCompletedError, ValidationError
from app.security.repository import SecurityRepository
from app.security.service import SecurityService


def main() -> int:
    """Ejecuta el bootstrap explícito sin imprimir datos sensibles."""
    settings = get_settings()
    if not settings.security_bootstrap_enabled:
        print("BOOTSTRAP_DISABLED")
        return 2
    if not settings.security_bootstrap_username or not settings.security_bootstrap_password:
        print("BOOTSTRAP_CONFIGURATION_REQUIRED")
        return 2

    service = SecurityService(SecurityRepository(settings.psycopg_conninfo))
    try:
        service.bootstrap_first_ti(
            username=settings.security_bootstrap_username,
            password=settings.security_bootstrap_password,
        )
    except BootstrapAlreadyCompletedError:
        print("BOOTSTRAP_ALREADY_COMPLETED")
        return 3
    except ValidationError:
        print("BOOTSTRAP_CONFIGURATION_INVALID")
        return 2
    print("BOOTSTRAP_COMPLETED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
