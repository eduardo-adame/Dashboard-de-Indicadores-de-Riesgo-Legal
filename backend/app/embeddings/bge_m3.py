"""Modelo de embeddings local (BGE-M3).

Decisiones de diseño que este módulo materializa:

* **Se ejecuta como librería dentro del propio servicio**, no como servicio de red
  independiente. Evita un salto de red por consulta, un contenedor adicional y la
  necesidad de sincronizar dos versiones del modelo.

* **Una sola instancia por proceso.** Cargar los pesos cuesta varios segundos y
  más de un gigabyte de memoria; hacerlo por petición sería inviable.

* **La misma instancia atiende indexación y consulta.** Fragmentos y consultas
  deben proyectarse en el mismo espacio vectorial; usar modelos o revisiones
  distintas produciría similitudes sin sentido.

* **Solo se produce la representación densa de 1.024 dimensiones.** El modelo
  también puede emitir pesos léxicos por término, pero la recuperación léxica la
  resuelve BM25 de forma independiente. Mezclar ambos mecanismos impediría
  razonar sobre la fusión de resultados y haría depender la búsqueda léxica de la
  disponibilidad del modelo.

* **Carga perezosa.** El servicio debe poder arrancar —y responder a las
  comprobaciones de salud— aunque el modelo todavía no se haya descargado.

* **Caché en ruta persistente.** El modelo ocupa varios gigabytes. Si la caché
  queda en el sistema de archivos efímero del contenedor, cada recreación provoca
  una descarga completa.

* **Una sola raíz de caché.** La ubicación se gobierna exclusivamente mediante
  ``HF_HUB_CACHE``; no se pasa ``cache_folder`` a la librería. Ese parámetro
  prevalecería sobre la variable de entorno y provocaría que el modelo se
  descargara dos veces dentro del mismo volumen.

* **La caché la puebla la librería, no una descarga manual del repositorio.**
  Debe evitarse pre-poblar la caché con una descarga del snapshot completo: eso
  añade artefactos que el servicio nunca utiliza (exportación ONNX, imágenes de
  documentación, pesos de las variantes sparse y colbert) y dispara la huella muy
  por encima de lo necesario. La librería descarga por sí misma solo lo que
  resuelve: el directorio raíz del snapshot y los módulos declarados en
  `modules.json`.

Alcance: este módulo no indexa, no persiste vectores y no implementa recuperación.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Sequence

from app.config import get_settings

logger = logging.getLogger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """El modelo de embeddings no está disponible en este proceso.

    El servicio sigue arrancando: la ausencia del modelo se reporta como
    indisponibilidad explícita y nunca se simula un resultado correcto.
    """


@dataclass(frozen=True)
class EmbeddingServiceStatus:
    """Estado observable del servicio de embeddings."""

    available: bool
    model_id: str
    dimensions: int | None
    cache_dir: str
    cache_dir_is_mount: bool
    load_count: int
    shared_instance: bool
    cache_root: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "model_id": self.model_id,
            "dimensions": self.dimensions,
            "cache_dir": self.cache_dir,
            "cache_dir_is_mount": self.cache_dir_is_mount,
            "cache_root": self.cache_root,
            "load_count": self.load_count,
            "shared_instance": self.shared_instance,
            "error": self.error,
        }


class BgeM3EmbeddingService:
    """Instancia lógica única del modelo de embeddings, compartida por el proceso."""

    def __init__(self) -> None:
        settings = get_settings()
        self._model_id = settings.bge_m3_model_id
        self._expected_dimensions = settings.embedding_dimensions
        self._cache_dir = settings.bge_m3_cache_dir
        self._model: Any = None
        self._load_error: str | None = None
        self._load_attempts = 0
        self._lock = threading.Lock()

    # -- propiedades de verificación ----------------------------------------
    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def cache_dir(self) -> str:
        return self._cache_dir

    @property
    def effective_cache_root(self) -> str:
        """Raíz efectiva de la caché del modelo (``HF_HUB_CACHE``).

        Es la única ubicación donde la librería busca y descarga el modelo.
        ``BGE_M3_CACHE_DIR`` es autoritativo y esta raíz se deriva de él, de modo
        que existe una sola fuente de verdad.

        Se expone para que el procedimiento de limpieza pueda confirmar
        empíricamente cuál es la raíz funcional en lugar de deducirla por el
        nombre de un directorio.
        """
        return os.path.join(self._cache_dir, "huggingface", "hub")

    @property
    def cache_dir_is_mount(self) -> bool:
        """True si la caché reside en un punto de montaje (volumen persistente)."""
        return os.path.ismount(self._cache_dir)

    @property
    def load_count(self) -> int:
        """Cargas efectivas del modelo en este proceso.

        Debe ser 0 (aún sin usar) o 1. Un valor mayor indica que el modelo se
        instanció más de una vez y que se está desperdiciando memoria.
        """
        return self._load_attempts

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    # -- carga perezosa ------------------------------------------------------
    def _configure_cache_env(self) -> None:
        """Dirige todas las descargas del modelo a la caché persistente.

        Las variables de caché de las librerías de modelos se derivan de
        ``BGE_M3_CACHE_DIR`` y se sobrescriben si apuntaban a otro sitio.
        ``HF_HUB_CACHE`` es la RAÍZ ÚNICA donde la librería busca y descarga el
        modelo.

        Esto no es cosmético. Si por ejemplo ``HF_HOME`` quedara apuntando al
        sistema de archivos efímero del contenedor, el modelo se re-descargaría
        en cada recreación, que es precisamente lo que la caché debe evitar. Un
        valor heredado del entorno es un modo de fallo silencioso, por eso se
        registra una advertencia cuando se corrige.
        """
        os.makedirs(self._cache_dir, exist_ok=True)

        # Todas las variables de caché convergen en UNA SOLA raíz.
        #
        # No basta con no pasar `cache_folder`: la librería cae entonces a
        # `SENTENCE_TRANSFORMERS_HOME`, que es el mismo parámetro por otra vía
        # (`if cache_folder is None: cache_folder = os.getenv(...)`). Si esa
        # variable apuntara a otro directorio, el modelo se descargaría dos veces
        # dentro del mismo volumen. Por eso se fijan todas al mismo valor.
        root = self.effective_cache_root
        derived = {
            "HF_HOME": os.path.join(self._cache_dir, "huggingface"),
            "HF_HUB_CACHE": root,
            "SENTENCE_TRANSFORMERS_HOME": root,
            "TRANSFORMERS_CACHE": root,
            # Sin telemetría: evita salidas de red no necesarias.
            "HF_HUB_DISABLE_TELEMETRY": "1",
        }
        for key, value in derived.items():
            existing = os.environ.get(key)
            if existing and os.path.normcase(existing) != os.path.normcase(value):
                logger.warning(
                    "%s apuntaba fuera de la caché persistente (%s); se redirige a %s",
                    key,
                    existing,
                    value,
                )
            os.environ[key] = value

    def _load(self) -> Any:
        """Carga el modelo una única vez. No es thread-safe por sí solo."""
        if self._model is not None:
            return self._model

        self._configure_cache_env()
        try:
            # Import tardío: permite que el módulo se importe (y el servicio
            # arranque) aunque las dependencias de ML no estén instaladas.
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            self._load_error = f"dependencias de embeddings no instaladas: {exc.__class__.__name__}"
            raise EmbeddingUnavailableError(self._load_error) from exc

        try:
            self._load_attempts += 1
            root = self.effective_cache_root
            logger.info("Cargando modelo %s desde la caché %s", self._model_id, root)
            # No se pasa `cache_folder`: ese parámetro prevalecería sobre
            # HF_HUB_CACHE y haría que la librería descargara en una segunda
            # ubicación dentro del mismo volumen. La caché se gobierna
            # exclusivamente por la variable de entorno, que es la única fuente
            # de verdad y la que apunta al volumen persistente.
            model = SentenceTransformer(self._model_id)
        except Exception as exc:  # noqa: BLE001 - frontera de dependencia externa
            # Se revierte el contador para no reportar una carga que no ocurrió.
            self._load_attempts = max(0, self._load_attempts - 1)
            self._load_error = f"{exc.__class__.__name__}: no se pudo cargar el modelo"
            raise EmbeddingUnavailableError(self._load_error) from exc

        observed = int(model.get_sentence_embedding_dimension())
        if observed != self._expected_dimensions:
            # La columna vectorial del almacén tiene una dimensión fija. Un modelo
            # con otra dimensionalidad produciría vectores incompatibles, así que
            # es preferible fallar de forma explícita antes que generar datos
            # inservibles.
            self._load_error = (
                f"dimensionalidad inesperada: {observed} != {self._expected_dimensions}"
            )
            self._model = None
            raise EmbeddingUnavailableError(self._load_error)

        self._model = model
        self._load_error = None
        logger.info("Modelo cargado con %d dimensiones", observed)
        return self._model

    def _get_model(self) -> Any:
        """Double-checked locking: garantiza una sola carga por proceso."""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    return self._load()
        return self._model

    # -- API de embeddings ---------------------------------------------------
    def _encode_dense(
        self,
        texts: Sequence[str],
        normalize_embeddings: bool = False,
    ) -> list[list[float]]:
        """Codificación densa compartida por indexación y consultas.

        ``normalize_embeddings`` se deja en ``False`` por defecto: la
        normalización depende de la métrica de similitud elegida y es parte del
        contrato de recuperación, no de la carga del modelo.
        """
        model = self._get_model()
        vectors = model.encode(
            list(texts),
            normalize_embeddings=normalize_embeddings,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        result: list[list[float]] = []
        for vec in vectors:
            floats = [float(x) for x in vec]
            if len(floats) != self._expected_dimensions:
                raise EmbeddingUnavailableError(
                    f"embedding con dimensión incorrecta: {len(floats)}"
                )
            result.append(floats)
        return result

    def encode_documents(
        self, texts: Sequence[str], normalize_embeddings: bool = False
    ) -> list[list[float]]:
        """Embeddings de fragmentos de documento.

        Usa la MISMA instancia de modelo que ``encode_query``.
        """
        return self._encode_dense(texts, normalize_embeddings)

    def encode_query(
        self, text: str, normalize_embeddings: bool = False
    ) -> list[float]:
        """Embedding de una consulta.

        Usa la MISMA instancia de modelo que ``encode_documents``.
        """
        return self._encode_dense([text], normalize_embeddings)[0]

    # -- estado ---------------------------------------------------------------
    def status(self) -> EmbeddingServiceStatus:
        """Estado sin forzar la carga del modelo."""
        return EmbeddingServiceStatus(
            available=self._model is not None,
            model_id=self._model_id,
            dimensions=self._expected_dimensions if self._model is not None else None,
            cache_dir=self._cache_dir,
            cache_dir_is_mount=self.cache_dir_is_mount,
            cache_root=self.effective_cache_root,
            load_count=self._load_attempts,
            shared_instance=True,
            error=self._load_error,
        )

    def probe(self) -> EmbeddingServiceStatus:
        """Fuerza la carga y una codificación mínima para verificar capacidad."""
        try:
            vector = self.encode_query("comprobación de capacidad del entorno")
        except EmbeddingUnavailableError as exc:
            return EmbeddingServiceStatus(
                available=False,
                model_id=self._model_id,
                dimensions=None,
                cache_dir=self._cache_dir,
                cache_dir_is_mount=self.cache_dir_is_mount,
                cache_root=self.effective_cache_root,
                load_count=self._load_attempts,
                shared_instance=True,
                error=str(exc),
            )
        return EmbeddingServiceStatus(
            available=True,
            model_id=self._model_id,
            dimensions=len(vector),
            cache_dir=self._cache_dir,
            cache_dir_is_mount=self.cache_dir_is_mount,
            cache_root=self.effective_cache_root,
            load_count=self._load_attempts,
            shared_instance=True,
            error=None,
        )


# ---------------------------------------------------------------------------
# Instancia única por proceso.
# ---------------------------------------------------------------------------
_service: BgeM3EmbeddingService | None = None
_service_lock = threading.Lock()


def get_embedding_service() -> BgeM3EmbeddingService:
    """Devuelve la única instancia lógica del modelo en este proceso."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = BgeM3EmbeddingService()
    return _service


def reset_embedding_service() -> None:
    """Reinicia la instancia única. Solo para pruebas."""
    global _service
    with _service_lock:
        _service = None
