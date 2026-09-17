"""Entorno Alembic para PostgreSQL/pgvector.

La URL se construye desde variables de entorno y nunca se registra con la
contraseña visible. Usa migraciones explícitas; no hay autogenerate ni ORM como
fuente normativa del esquema.
"""
from __future__ import annotations

from logging.config import fileConfig
import os
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def database_url() -> str:
    """Construye la URL de migración desde el entorno del backend."""
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = quote_plus(os.getenv("POSTGRES_USER", "app"))
    password = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
    database = quote_plus(os.getenv("POSTGRES_DB", "riesgo_legal"))
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema="public",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
