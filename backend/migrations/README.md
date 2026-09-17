# Migraciones de base de datos

Las revisiones son lineales y autocontenidas:

1. `0001_foundation`
2. `0002_ingestion_domain`
3. `0003_documents_corpus`
4. `0004_analytics_rag_jobs`
5. `0005_security_audit_grants`

Ejecutar desde `backend/` con las variables `POSTGRES_*` del entorno:

```text
alembic upgrade head
alembic downgrade -1
```

Los downgrades se destinan a bases desechables de desarrollo. No eliminan los
objetos binarios del filesystem y, una vez existan datos reales, la política es
corregir hacia adelante.
