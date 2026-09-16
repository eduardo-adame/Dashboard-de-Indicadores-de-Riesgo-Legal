"""Comprobación de conectividad con PostgreSQL y disponibilidad de pgvector.

Este módulo NO crea tablas, esquemas, tipos ni migraciones, y tampoco habilita
permanentemente la extensión `vector`. La instalación de la extensión y la
definición del modelo de datos son responsabilidad de las migraciones de la
aplicación, que se ejecutan en una etapa posterior y separada del arranque.

Comprobar la capacidad vectorial dentro de una transacción revertida permite
validar que la imagen de base de datos es apta sin dejar rastro alguno.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatabaseStatus:
    """Resultado de la comprobación de conectividad.

    Semántica de las banderas:

    * ``pgvector_available_in_image`` — la extensión está disponible en la
      instalación de PostgreSQL (``pg_available_extensions``).
    * ``pgvector_installed`` — la extensión está habilitada en esta base
      (``pg_extension``). Permanecerá en ``False`` hasta que las migraciones la
      habiliten, por lo que no debe tratarse como un fallo del arranque.
    * ``vector_1024_accepted`` — el tipo vector acepta 1.024 dimensiones; solo es
      evaluable si la extensión está habilitada.

    ``healthy`` expresa la readiness del servicio: que alcance la base de datos.
    No exige la extensión habilitada, porque una base de datos recién creada
    todavía no la tiene y eso no impide operar al servicio.
    """

    reachable: bool
    server_version: str | None = None
    pgvector_available_in_image: bool = False
    pgvector_installed: bool = False
    pgvector_version: str | None = None
    vector_1024_accepted: bool = False
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """Readiness del servicio: conectividad efectiva con la base de datos."""
        return self.reachable

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "reachable": self.reachable,
            "server_version": self.server_version,
            "pgvector_available_in_image": self.pgvector_available_in_image,
            "pgvector_installed": self.pgvector_installed,
            "pgvector_version": self.pgvector_version,
            "vector_1024_accepted": self.vector_1024_accepted,
            "healthy": self.healthy,
            # El error se sanitiza: nunca debe revelar la contraseña.
            "error": self.error,
        }
        if self.reachable and not self.pgvector_installed:
            payload["note"] = (
                "pgvector está disponible en la imagen pero todavía no habilitado en la base; "
                "su habilitación corresponde a las migraciones de la aplicación."
            )
        return payload


def check_database() -> DatabaseStatus:
    """Comprueba conectividad y disponibilidad de pgvector sin crear estructura."""
    settings = get_settings()
    try:
        with psycopg.connect(settings.psycopg_conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version")
                row = cur.fetchone()
                server_version = str(row[0]) if row else None

                # La extensión viene con la instalación de PostgreSQL; aquí solo
                # se consulta su disponibilidad, no se instala.
                cur.execute(
                    "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'"
                )
                avail = cur.fetchone()
                pgvector_available_in_image = bool(avail) and int(avail[0]) > 0

                cur.execute(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
                ext = cur.fetchone()
                pgvector_installed = ext is not None
                pgvector_version = str(ext[0]) if ext else None

                vector_1024_accepted = False
                if pgvector_installed:
                    dims = settings.embedding_dimensions
                    literal = "[" + ",".join(["0"] * dims) + "]"
                    cur.execute("SELECT vector_dims(CAST(%s AS vector))", (literal,))
                    dim_row = cur.fetchone()
                    vector_1024_accepted = bool(dim_row) and int(dim_row[0]) == dims

            # Transacción de solo lectura: se revierte para no dejar rastro.
            conn.rollback()

        return DatabaseStatus(
            reachable=True,
            server_version=server_version,
            pgvector_available_in_image=pgvector_available_in_image,
            pgvector_installed=pgvector_installed,
            pgvector_version=pgvector_version,
            vector_1024_accepted=vector_1024_accepted,
        )
    except Exception as exc:  # noqa: BLE001 - frontera de servicio
        # No se registra el conninfo: podría contener la contraseña.
        logger.warning("Comprobación de base de datos fallida: %s", exc.__class__.__name__)
        return DatabaseStatus(
            reachable=False,
            error=f"{exc.__class__.__name__}: conectividad no disponible",
        )
