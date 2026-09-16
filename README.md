# Dashboard de Indicadores de Riesgo Legal — `producto/`

Repositorio Git del sistema. Aplicación web analítica para consolidar, procesar y
visualizar información de riesgo contractual, litigios, cumplimiento normativo,
eficiencia operativa y calidad documental.

---

## 1. Estado actual: entorno de desarrollo

Este repositorio contiene la **estructura mínima de arranque** del sistema.

**No hay funcionalidad de negocio implementada.** En concreto, todavía no existen:
ingesta de datos, validación y cuarentena, procesamiento documental y OCR, cálculo
de indicadores, análisis de tendencias, búsqueda semántica, control de acceso ni
auditoría. Tampoco existe el modelo de datos de la aplicación.

Del mismo modo, el backend no crea tablas, esquemas ni migraciones: la definición
del modelo de datos se incorporará en una etapa posterior.

Las rutas HTTP expuestas por el backend (`/health`, `/health/ready`,
`/health/embeddings`) son una decisión de diseño de este servicio, cuya finalidad
es permitir comprobar el estado del proceso, de sus dependencias y del modelo de
embeddings.

---

## 2. Requisitos del equipo

Configuración de referencia del proyecto:

| Recurso | Referencia |
|---|---|
| RAM | **16 GB** (8 GB PostgreSQL/pgvector + 4 GB backend + 4 GB servicios auxiliares y SO) |
| Almacenamiento | **SSD NVMe con ≥50 GB disponibles** |

La distribución 8/4/4 es una **referencia de verificación**, no un límite funcional
impuesto por el sistema. Los límites de memoria del Compose son techos de
implementación y pueden reducirse desde `.env` en equipos con menos recursos.

Software necesario: Docker Engine + Docker Compose v2, Python ≥3.10 (para los smoke
tests) y Node 22 (solo si se desarrolla el frontend fuera del contenedor).

> **Equipos por debajo de la referencia:** el stack arranca, pero la carga del
> modelo de embeddings (~2.3 GB de descarga y ~1.3 GB de memoria) puede no ser
> viable. Ver sección 7.

---

## 3. Puesta en marcha

```bash
cd producto
copy .env.example .env      # Linux/macOS: cp .env.example .env
```

Editar `.env` y asignar valores locales a las variables marcadas como
**OBLIGATORIA**:

- `POSTGRES_PASSWORD`
- `AIRFLOW_ADMIN_PASSWORD` (solo si se activa el perfil `airflow`)

`.env` está excluido de Git por `.gitignore` y **nunca** debe versionarse.
`.env.example` sí se versiona y solo contiene placeholders.

Compose falla de forma explícita si faltan esas variables (sintaxis `${VAR:?...}`),
en lugar de arrancar con credenciales por defecto.

> **Si se regenera `.env` con una contraseña nueva de PostgreSQL**, el volumen
> `riesgo-legal-pgdata` conserva la contraseña con la que se inicializó y el
> backend dejará de autenticarse (la readiness pasará a `503`). La contraseña de
> PostgreSQL solo se aplica al crear el volumen por primera vez. Para adoptar una
> contraseña nueva hay que eliminar el volumen de datos, sin tocar los demás:
>
> ```bash
> docker compose --profile airflow down
> docker volume rm riesgo-legal-pgdata    # solo los datos de PostgreSQL
> docker compose --profile airflow up -d
> ```
>
> El volumen de la caché del modelo y el de metadatos de Airflow no se ven
> afectados.

---

## 4. Arranque selectivo

Los servicios de Airflow están tras el perfil `airflow`, de modo que el trabajo
diario no obliga a mantener activos los componentes más pesados. El perfil no
elimina ningún servicio: el stack completo sigue siendo el mismo.

```bash
# Desarrollo ligero: frontend + backend + postgres
docker compose up -d

# Stack completo: incluye Airflow
docker compose --profile airflow up -d

# Estado
docker compose ps
docker compose --profile airflow ps

# Detener (CONSERVA volúmenes: no re-descarga el modelo)
docker compose --profile airflow down

# Detener y DESTRUIR volúmenes (implica re-descargar el modelo, ~2.3 GB)
docker compose --profile airflow down -v
```

---

## 5. Topología

Cuatro **servicios lógicos**:

| # | Servicio lógico | Contenedores | Imagen | Puerto por defecto |
|---|---|---|---|---|
| 1 | `postgres` | 1 | `pgvector/pgvector:pg16` | 5432 |
| 2 | `backend` | 1 | build `./backend` | 8000 |
| 3 | `frontend` | 1 | build `./frontend` (nginx) | 3000 |
| 4 | `airflow` | 3 (`init`, `scheduler`, `webserver`) | `apache/airflow:2.10.5-python3.12` | 8080 |

`airflow` es **un único servicio lógico** materializado en varios contenedores de la
misma imagen. Los tres llevan la etiqueta `riesgo-legal.logical-service: airflow`,
lo que permite comprobar que el recuento de servicios lógicos sigue siendo cuatro.

El sistema **no** requiere Redis, RabbitMQ, Elasticsearch, nginx como servicio
independiente, un segundo PostgreSQL ni un servicio de embeddings.

### Volúmenes persistentes (independientes entre sí)

| Volumen | Montaje | Contenido |
|---|---|---|
| `riesgo-legal-pgdata` | `/var/lib/postgresql/data` | Datos de PostgreSQL |
| `riesgo-legal-bge-cache` | `/home/appuser/.cache/bge-m3` | Caché del modelo de embeddings |
| `riesgo-legal-airflow-db` | `/opt/airflow/sqlite_db` | Metadatos SQLite de Airflow |
| `riesgo-legal-airflow-logs` | `/opt/airflow/logs` | Logs de Airflow |

Los volúmenes se mantienen separados a propósito: los datos de la base, la caché
del modelo y los metadatos de la orquestación tienen ciclos de vida distintos y no
deben compartir almacenamiento.

Los metadatos de Airflow usan SQLite en su propio volumen, no la base de datos de
la aplicación.

Ningún volumen se versiona en Git.

---

## 6. Verificación del entorno

Batería única de smoke tests (solo librería estándar de Python):

```bash
# Comprobaciones de repositorio y topología, sin requerir el stack en ejecución
python scripts/smoke.py --static-only

# Stack ligero en ejecución
python scripts/smoke.py

# Stack completo (incluye Airflow)
python scripts/smoke.py --profile airflow

# Omitir la comprobación del modelo de embeddings (descarga pesada)
python scripts/smoke.py --profile airflow --skip-model
```

Cada comprobación devuelve `PASS` / `FAIL` / `SKIP` con su evidencia. El script
**nunca convierte un `SKIP` en `PASS`**: si una evidencia no pudo obtenerse, se
reporta como pendiente. El código de salida es `0` solo si no hay `FAIL`.

Pruebas unitarias del backend (en el equipo o en CI, no dentro de la imagen):

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate    # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -v
```

Las pruebas están marcadas como `contract` (comportamiento requerido) o
`robustness` (controles defensivos).

---

## 7. Modelo de embeddings (BGE-M3)

- Se ejecuta como **librería local dentro del contenedor `backend`**.
- **No existe** un servicio ni contenedor independiente de embeddings.
- Se carga **una sola vez por proceso** para no recargar los pesos en cada petición.
- Utiliza el **volumen persistente** `riesgo-legal-bge-cache`, de modo que no se
  re-descarga entre recreaciones del contenedor.
- Produce vectores de **1.024 dimensiones**.
- La **misma instancia** atiende la codificación de documentos y de consultas, de
  forma que ambas se proyectan en el mismo espacio vectorial.
- **No sustituye a BM25**: solo se usa la representación densa. La recuperación
  léxica es un mecanismo independiente y no depende de la disponibilidad del modelo.

El modelo **no** se hornea en la imagen: se descarga en el volumen en el primer uso.
Por eso el backend arranca aunque el modelo aún no esté disponible, y en ese caso lo
reporta como indisponibilidad explícita en lugar de simular un resultado correcto.

Comprobación de capacidad:

```bash
curl -X POST http://localhost:8000/health/embeddings/probe
```

> **Coste:** la primera invocación descarga ~2.3 GB en el volumen y consume ~1.3 GB
> de memoria. En equipos por debajo de la configuración de referencia esta
> comprobación puede no ser viable y debe reportarse como evidencia pendiente, no
> como un fallo del sistema.

---

## 8. Endpoints de salud

| Método | Ruta | Propósito |
|---|---|---|
| `GET` | `/health` | Liveness del backend. No depende de servicios externos. |
| `GET` | `/health/ready` | Readiness: conectividad con PostgreSQL y disponibilidad de pgvector. |
| `GET` | `/health/embeddings` | Estado del modelo **sin** forzar su carga. |
| `POST` | `/health/embeddings/probe` | Fuerza la carga y verifica 1.024 dimensiones. |
| `GET` | `/health` (frontend) | Liveness de nginx, independiente del backend. |

Estas rutas son una decisión de diseño del servicio.

---

## 9. Seguridad

- `.env` **nunca** se versiona; `.gitignore` lo excluye explícitamente.
- `.env.example` solo contiene placeholders identificables como `CHANGE_ME_...`.
- Sin credenciales por defecto: Compose exige definirlas.
- El backend se ejecuta con un usuario **sin privilegios de root** (`appuser`, UID 10001).
- CORS restringido a orígenes explícitos; sin comodín y sin credenciales.
- Los mensajes de error no filtran la contraseña de la base de datos.
- nginx no expone archivos ocultos y aplica cabeceras básicas de endurecimiento.

La autenticación y el control de acceso forman parte de una etapa posterior y
**no** están presentes todavía.

---

## 10. Flujo de trabajo con Git

- Rama principal: **`main`**.
- Los cambios deben ser revisables y reversibles de forma aislada.
- Convención de mensajes: `<tipo>(<alcance>): <descripción>`, con tipos como
  `feat`, `fix`, `test`, `refactor`, `docs`, `build`, `ci`, `chore`, `perf`.
- No se versiona ningún secreto ni archivo generado (véase `.gitignore`).

---

## 11. Estructura

```text
producto/
├── .env                  ← local, NO versionado
├── .env.example          ← versionado, solo placeholders
├── .gitignore
├── docker-compose.yml    ← los 4 servicios lógicos
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt          núcleo (FastAPI, uvicorn, psycopg)
│   ├── requirements-ml.txt       embeddings (sentence-transformers)
│   ├── requirements-dev.txt      pruebas (pytest, httpx)
│   ├── pytest.ini
│   ├── app/
│   │   ├── main.py               endpoints de salud
│   │   ├── config.py             configuración desde entorno
│   │   ├── db.py                 conectividad y disponibilidad de pgvector
│   │   └── embeddings/bge_m3.py  modelo local, instancia única, caché persistente
│   └── tests/
├── frontend/
│   ├── Dockerfile                multi-stage: build Node → servicio nginx
│   ├── nginx.conf
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   ├── src/{main.jsx,App.jsx,styles.css}
│   └── tests/
└── scripts/
    └── smoke.py                  batería única de verificación
```
