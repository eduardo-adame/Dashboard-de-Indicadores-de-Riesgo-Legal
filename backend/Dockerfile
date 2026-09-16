# Backend — Dashboard de Indicadores de Riesgo Legal
#
# Imagen de desarrollo. BGE-M3 se instala como librería local (CPU). El modelo no
# se hornea en la imagen: se descarga en el volumen persistente `bge_cache` en el
# primer uso, de modo que la imagen permanece pequeña y la caché sobrevive a las
# recreaciones del contenedor.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias de sistema mínimas para compilar las dependencias de Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# PyTorch CPU-only: evita los wheels CUDA (~2.5 GB) que este servicio no necesita,
# ya que la inferencia del modelo de embeddings se ejecuta en CPU.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -r requirements-ml.txt

# Usuario sin privilegios de root: el servicio no necesita permisos elevados.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Caché persistente del modelo. La ruta debe coincidir con el punto de montaje del
# volumen `bge_cache`; si diverge, el modelo se re-descarga en cada recreación.
ENV BGE_M3_CACHE_DIR=/home/appuser/.cache/bge-m3 \
    HF_HOME=/home/appuser/.cache/bge-m3/huggingface
RUN mkdir -p /home/appuser/.cache/bge-m3

COPY --chown=appuser:appuser ./app ./app

# Las pruebas no se incluyen en la imagen de runtime (ver .dockerignore): se
# ejecutan en el host o en CI con requirements-dev.txt. La verificación de
# capacidad del modelo dentro del contenedor se realiza por HTTP mediante
# POST /health/embeddings/probe.
EXPOSE 8000

# Healthcheck sin dependencias externas: usa la stdlib de Python.
HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=5 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
