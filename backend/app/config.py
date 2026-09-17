"""Configuración del backend leída desde variables de entorno.

Todos los valores tienen un valor por defecto apto para desarrollo y no contienen
secretos: las credenciales reales viven en el archivo `.env`, excluido de Git.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración de entorno del backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Identidad del servicio -------------------------------------------
    app_name: str = "backend-riesgo-legal"
    environment: str = "development"

    # --- Persistencia (PostgreSQL + pgvector) ------------------------------
    # Se usa para comprobar conectividad y disponibilidad de la extensión
    # vectorial. El arranque de la API no ejecuta migraciones automáticamente;
    # el esquema se administra de forma explícita mediante Alembic.
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "app"
    postgres_password: str = ""
    postgres_db: str = "riesgo_legal"

    # --- Embeddings BGE-M3 --------------------------------------------------
    bge_m3_model_id: str = "BAAI/bge-m3"
    # Ruta dentro del contenedor; debe coincidir con el punto de montaje del
    # volumen de caché definido en docker-compose.yml.
    bge_m3_cache_dir: str = "/home/appuser/.cache/bge-m3"
    # Dimensionalidad de salida del modelo. Es una propiedad del modelo, no un
    # parámetro libre: la aplicación rechaza cualquier vector de otra longitud.
    embedding_dimensions: int = 1024
    # Si es False, el servicio arranca aunque el modelo no esté disponible.
    bge_m3_required_at_startup: bool = False

    # --- CORS ---------------------------------------------------------------
    # Orígenes permitidos explícitos. No se usa "*" por defecto: permitir
    # cualquier origen en un servicio que pronto manejará datos protegidos es un
    # riesgo innecesario.
    backend_cors_origins: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        """Lista de orígenes CORS a partir de la cadena separada por comas."""
        return [origin.strip() for origin in self.backend_cors_origins.split(",") if origin.strip()]

    @property
    def sqlalchemy_style_dsn(self) -> str:
        """DSN legible para logs. Nunca incluye la contraseña."""
        return (
            f"postgresql://{self.postgres_user}:***"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def psycopg_conninfo(self) -> str:
        """Conninfo para psycopg. La contraseña no se registra en logs."""
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"user={self.postgres_user} password={self.postgres_password} "
            f"dbname={self.postgres_db} connect_timeout=5"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Devuelve una única instancia de configuración por proceso."""
    return Settings()
