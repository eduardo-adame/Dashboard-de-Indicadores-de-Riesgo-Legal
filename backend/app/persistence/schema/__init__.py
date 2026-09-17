"""Convenciones públicas del esquema físico."""

APP_SCHEMA = "app"
AUDIT_SCHEMA = "audit"
EMBEDDING_DIMENSIONS = 1024

ALEMBIC_REVISIONS = (
    "0001_foundation",
    "0002_ingestion_domain",
    "0003_documents_corpus",
    "0004_analytics_rag_jobs",
    "0005_security_audit_grants",
)

RAG_STATES = (
    "EVIDENCIA_SUFICIENTE",
    "EVIDENCIA_INSUFICIENTE",
    "SIN_EVIDENCIA",
    "SIN_AUTORIZACION",
)
