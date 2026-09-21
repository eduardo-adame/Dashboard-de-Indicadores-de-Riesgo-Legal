"""Aplicación FastAPI del backend.

Esta aplicación expone comprobaciones de salud, capacidad del modelo de
embeddings y operaciones protegidas de seguridad.

Las rutas y los códigos de estado definidos aquí son una decisión de diseño de
este servicio. Su finalidad es permitir que un orquestador de contenedores y un
desarrollador determinen si el proceso está vivo, si alcanza sus dependencias y
si el modelo de embeddings puede cargarse.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.coordination.api import router as coordination_router
from app.db import check_database
from app.embeddings import EmbeddingUnavailableError, get_embedding_service
from app.ingestion.api import router as ingestion_router
from app.security.api import router as security_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_START_TIME = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranque y cierre del servicio."""
    settings = get_settings()
    logger.info(
        "Iniciando %s (environment=%s)", settings.app_name, settings.environment
    )
    if settings.bge_m3_required_at_startup:
        # Desactivado por defecto: forzar la descarga del modelo en el arranque
        # hace que el servicio dependa de la red para poder iniciar, lo que es
        # frágil en desarrollo y en despliegues sin caché previa.
        try:
            get_embedding_service().probe()
        except EmbeddingUnavailableError as exc:
            logger.error("El modelo se marcó como obligatorio en arranque pero no está disponible: %s", exc)
    yield
    logger.info("Deteniendo %s", settings.app_name)


app = FastAPI(
    title="Backend — Dashboard de Indicadores de Riesgo Legal",
    description=(
        "Servicio base con comprobaciones de salud y verificación de capacidad "
        "del modelo de embeddings."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# CORS conserva orígenes explícitos y habilita credenciales solo para la cookie
# de refresh; el token de acceso se presenta mediante Authorization Bearer.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT"],
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
)

app.include_router(security_router)
app.include_router(ingestion_router)
app.include_router(coordination_router)


@app.get("/health", tags=["health"])
async def health() -> dict[str, Any]:
    """Liveness: el proceso está vivo y sirviendo.

    No depende de servicios externos, de modo que un fallo de la base de datos o
    la ausencia del modelo no enmascaren el estado del propio contenedor.
    """
    settings = get_settings()
    embeddings = get_embedding_service().status()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
        "uptime_seconds": round(time.monotonic() - _START_TIME, 3),
        "embeddings_loaded": embeddings.available,
    }


@app.get("/health/ready", tags=["health"])
async def ready(response: Response) -> JSONResponse:
    """Readiness: conectividad con PostgreSQL y disponibilidad de pgvector.

    La comprobación vectorial evalúa un literal de 1.024 dimensiones sin crear
    tablas, columnas ni tipos, y la instalación permanente de la extensión no se
    realiza aquí.
    """
    db = check_database().as_dict()
    payload: dict[str, Any] = {
        "status": "ok" if db["healthy"] else "degraded",
        "database": db,
    }
    return JSONResponse(status_code=200 if db["healthy"] else 503, content=payload)


@app.get("/health/embeddings", tags=["health"])
async def embeddings_status() -> dict[str, Any]:
    """Estado del modelo de embeddings sin forzar su carga."""
    return get_embedding_service().status().as_dict()


@app.post("/health/embeddings/probe", tags=["health"])
async def embeddings_probe() -> JSONResponse:
    """Verifica la capacidad del modelo de embeddings.

    Fuerza la carga perezosa del modelo y codifica un texto de prueba para
    comprobar que la dimensionalidad es la esperada (1.024) y que el modelo se
    cargó una sola vez en este proceso.

    Se expone como POST porque puede provocar la descarga del modelo en el
    volumen de caché: no debe dispararse como efecto colateral de un GET. No
    indexa, no recupera y no persiste embeddings.
    """
    service = get_embedding_service()
    status = service.probe()
    payload = status.as_dict()
    payload["status"] = "ok" if status.available else "unavailable"

    if not status.available:
        # Indisponibilidad explícita: la respuesta nunca debe presentarse como si
        # el resultado se hubiera generado correctamente.
        payload["detail"] = (
            "El modelo de embeddings no está disponible en este proceso. "
            "La operación solicitada no pudo completarse."
        )
        return JSONResponse(status_code=503, content=payload)

    payload["loaded_once_per_process"] = status.load_count == 1
    return JSONResponse(status_code=200, content=payload)
