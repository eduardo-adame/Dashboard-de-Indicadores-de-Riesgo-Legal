"""Configuración del backend leída desde variables de entorno.

Todos los valores tienen un valor por defecto apto para desarrollo y no contienen
secretos: las credenciales reales viven en el archivo `.env`, excluido de Git.
"""
from __future__ import annotations

import json
from functools import lru_cache

from pydantic import field_validator
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

    # --- Autenticación y sesiones -----------------------------------------
    # El keyring se recibe como objeto JSON ``{\"kid\": \"secreto-base64url\"}``.
    # No hay una clave de reserva en código: un entorno que atienda usuarios
    # debe aportar su material criptográfico mediante configuración protegida.
    security_jwt_issuer: str = "riesgo-legal-backend"
    security_jwt_audience: str = "riesgo-legal-web"
    security_jwt_keyring_json: str = "{}"
    security_jwt_active_kid: str = ""
    security_refresh_cookie_name: str = "riesgo_legal_refresh"
    security_refresh_cookie_secure: bool | None = None
    security_bootstrap_enabled: bool = False
    security_bootstrap_username: str | None = None
    security_bootstrap_password: str | None = None

    # --- Ingesta de archivos ---------------------------------------------
    ingestion_storage_root: str = ".data/ingestion"
    ingestion_controlled_root: str = ".data/controlled"
    ingestion_max_file_bytes: int = 52_428_800
    ingestion_max_archive_entries: int = 2_000
    ingestion_max_archive_uncompressed_bytes: int = 268_435_456
    ingestion_max_archive_entry_bytes: int = 67_108_864
    ingestion_max_compression_ratio: float = 100.0
    ingestion_csv_sample_bytes: int = 65_536
    ingestion_stream_chunk_bytes: int = 1_048_576
    ingestion_max_tabular_rows: int = 250_000
    ingestion_max_tabular_columns: int = 256
    ingestion_max_tabular_cells: int = 5_000_000

    @field_validator(
        "ingestion_max_file_bytes",
        "ingestion_max_archive_entries",
        "ingestion_max_archive_uncompressed_bytes",
        "ingestion_max_archive_entry_bytes",
        "ingestion_csv_sample_bytes",
        "ingestion_stream_chunk_bytes",
        "ingestion_max_tabular_rows",
        "ingestion_max_tabular_columns",
        "ingestion_max_tabular_cells",
    )
    @classmethod
    def validate_positive_ingestion_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Los límites de ingesta deben ser positivos")
        return value

    @field_validator("ingestion_max_compression_ratio")
    @classmethod
    def validate_compression_ratio(cls, value: float) -> float:
        if value <= 1:
            raise ValueError("La relación máxima de compresión debe ser mayor que uno")
        return value

    @field_validator("security_jwt_keyring_json")
    @classmethod
    def validate_keyring_json(cls, value: str) -> str:
        """Comprueba que el formato de configuración no sea ambiguo."""
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("SECURITY_JWT_KEYRING_JSON debe ser JSON válido") from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(kid, str) and isinstance(secret, str)
            for kid, secret in parsed.items()
        ):
            raise ValueError("SECURITY_JWT_KEYRING_JSON debe ser un objeto de claves de texto")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        """Lista de orígenes CORS a partir de la cadena separada por comas."""
        return [origin.strip() for origin in self.backend_cors_origins.split(",") if origin.strip()]

    @property
    def jwt_keyring(self) -> dict[str, str]:
        """Devuelve únicamente claves configuradas localmente."""
        return dict(json.loads(self.security_jwt_keyring_json))

    @property
    def refresh_cookie_secure(self) -> bool:
        """Exige la marca Secure fuera del entorno de desarrollo."""
        if self.security_refresh_cookie_secure is not None:
            return self.security_refresh_cookie_secure
        return self.environment.lower() != "development"

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
