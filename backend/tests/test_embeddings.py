"""Pruebas del servicio de embeddings.

Se distinguen dos tipos:

* ``contract`` — la dimensionalidad de 1.024 es una propiedad fija del modelo y
  del almacén vectorial; un vector de otra longitud es inservible.
* ``robustness`` — carga local, instancia única por proceso, caché persistente y
  separación de la recuperación léxica son decisiones de diseño que conviene
  proteger contra regresiones.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from app import config as config_module
from app.embeddings import bge_m3 as embeddings_module
from app.embeddings.bge_m3 import (
    BgeM3EmbeddingService,
    EmbeddingUnavailableError,
    get_embedding_service,
    reset_embedding_service,
)

EXPECTED_DIMENSIONS = 1024


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Aísla configuración y estado entre pruebas.

    La caché se dirige a un directorio temporal para no tocar el volumen real ni
    requerir permisos del sistema.
    """
    cache_dir = tmp_path / "bge-cache"
    monkeypatch.setenv("BGE_M3_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("BGE_M3_MODEL_ID", "BAAI/bge-m3")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(EXPECTED_DIMENSIONS))
    monkeypatch.setenv("POSTGRES_PASSWORD", "placeholder-solo-para-pruebas")

    # Hermeticidad: eliminar cualquier valor heredado de otras pruebas o del host.
    for var in (
        "HF_HOME",
        "HF_HUB_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
        "TRANSFORMERS_CACHE",
        "HF_HUB_DISABLE_TELEMETRY",
    ):
        monkeypatch.delenv(var, raising=False)

    config_module.get_settings.cache_clear()
    reset_embedding_service()
    yield
    config_module.get_settings.cache_clear()
    reset_embedding_service()


def _install_fake_sentence_transformers(
    monkeypatch: pytest.MonkeyPatch, dimension: int = EXPECTED_DIMENSIONS
) -> dict[str, int]:
    """Instala un `sentence_transformers` falso para ejercitar la lógica real.

    Permite probar el camino completo de carga (instancia única, guarda de
    dimensionalidad, configuración de caché) sin descargar el modelo real, que
    ocupa varios gigabytes.
    """
    counter = {"instances": 0, "encode_calls": 0}

    class _FakeSentenceTransformer:
        def __init__(self, model_id: str, cache_folder: str | None = None) -> None:
            counter["instances"] += 1
            self.model_id = model_id
            self.cache_folder = cache_folder

        def get_sentence_embedding_dimension(self) -> int:
            return dimension

        def encode(self, texts, **kwargs):  # noqa: ANN001, ANN202
            counter["encode_calls"] += 1
            return [[0.5] * dimension for _ in texts]

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    return counter


# ---------------------------------------------------------------------------
# Contrato de dimensionalidad
# ---------------------------------------------------------------------------
@pytest.mark.contract
@pytest.mark.requires_model
def test_embedding_dimension_is_1024() -> None:
    """El modelo produce vectores de exactamente 1.024 dimensiones.

    Usa el modelo real. Se omite cuando el modelo no está disponible en la
    máquina, en cuyo caso la evidencia queda pendiente y nunca como superada.
    """
    service = get_embedding_service()
    try:
        vector = service.encode_query("texto de comprobación de dimensionalidad")
    except EmbeddingUnavailableError as exc:
        pytest.skip(f"Modelo no disponible en esta máquina: {exc}")

    assert len(vector) == EXPECTED_DIMENSIONS
    assert all(isinstance(x, float) for x in vector)
    assert service.status().dimensions == EXPECTED_DIMENSIONS


@pytest.mark.contract
def test_embedding_dimension_contract_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La guarda de dimensionalidad rechaza cualquier modelo que no dé 1.024.

    Complemento determinista del test anterior: verifica que la comprobación está
    efectivamente impuesta en el código, sin depender de la descarga del modelo.
    """
    _install_fake_sentence_transformers(monkeypatch, dimension=EXPECTED_DIMENSIONS)
    service = get_embedding_service()
    vector = service.encode_query("consulta")
    assert len(vector) == EXPECTED_DIMENSIONS

    # Un modelo de otra dimensionalidad debe ser rechazado, no tolerado.
    reset_embedding_service()
    _install_fake_sentence_transformers(monkeypatch, dimension=768)
    with pytest.raises(EmbeddingUnavailableError, match="dimensionalidad inesperada"):
        get_embedding_service().encode_query("consulta")


# ---------------------------------------------------------------------------
# Robustez
# ---------------------------------------------------------------------------
@pytest.mark.robustness
def test_model_is_loaded_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """El modelo se instancia una sola vez por proceso.

    Evita recargar los pesos, que ocupan más de un gigabyte de memoria, en cada
    petición.
    """
    counter = _install_fake_sentence_transformers(monkeypatch)
    service = get_embedding_service()

    service.encode_query("primera consulta")
    service.encode_documents(["doc a", "doc b"])
    service.encode_query("segunda consulta")

    assert counter["instances"] == 1, "el modelo se instanció más de una vez"
    assert service.load_count == 1
    assert counter["encode_calls"] == 3


@pytest.mark.robustness
def test_same_instance_serves_documents_and_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indexación y consulta comparten instancia.

    Fragmentos y consultas deben proyectarse en el mismo espacio vectorial; usar
    modelos distintos produciría similitudes sin sentido.
    """
    counter = _install_fake_sentence_transformers(monkeypatch)
    service = get_embedding_service()

    query_vector = service.encode_query("consulta")
    document_vectors = service.encode_documents(["fragmento uno", "fragmento dos"])

    assert counter["instances"] == 1
    assert service.status().shared_instance is True
    assert len(query_vector) == EXPECTED_DIMENSIONS
    assert len(document_vectors) == 2
    assert all(len(vector) == EXPECTED_DIMENSIONS for vector in document_vectors)


@pytest.mark.robustness
def test_service_is_singleton() -> None:
    """Existe una única instancia lógica del servicio por proceso."""
    assert get_embedding_service() is get_embedding_service()


@pytest.mark.robustness
def test_cache_environment_points_to_persistent_dir() -> None:
    """La configuración de caché apunta a la ruta persistente configurada."""
    service = get_embedding_service()
    cache_dir = service.cache_dir

    assert cache_dir == os.environ["BGE_M3_CACHE_DIR"]
    assert cache_dir

    service._configure_cache_env()  # noqa: SLF001 - verificación interna
    assert os.path.isdir(cache_dir)
    assert os.environ.get("HF_HOME", "").startswith(cache_dir)
    assert os.environ.get("SENTENCE_TRANSFORMERS_HOME", "").startswith(cache_dir)


@pytest.mark.robustness
def test_cache_env_overrides_stale_inherited_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresión: una variable heredada no puede desviar la caché fuera del volumen.

    Si la caché quedara fuera del almacenamiento persistente, el modelo se
    re-descargaría completo en cada recreación del contenedor.
    """
    service = get_embedding_service()
    monkeypatch.setenv("HF_HOME", "/ruta/efimera/fuera/del/volumen")
    monkeypatch.setenv("SENTENCE_TRANSFORMERS_HOME", "/otra/ruta/efimera")

    service._configure_cache_env()  # noqa: SLF001 - verificación interna

    assert os.environ["HF_HOME"] == os.path.join(service.cache_dir, "huggingface")
    assert os.environ["SENTENCE_TRANSFORMERS_HOME"] == os.path.join(
        service.cache_dir, "sentence_transformers"
    )
    assert os.environ["HF_HOME"].startswith(service.cache_dir)


@pytest.mark.robustness
def test_model_load_is_lazy() -> None:
    """El servicio arranca sin el modelo: la carga ocurre en el primer uso."""
    service = get_embedding_service()
    status = service.status()

    assert status.available is False
    assert status.load_count == 0
    assert service.is_loaded is False


@pytest.mark.robustness
def test_lexical_retrieval_is_not_delegated_to_the_model() -> None:
    """El servicio de embeddings no expone recuperación léxica.

    La recuperación léxica la resuelve BM25 de forma independiente. Exponer aquí
    pesos por término acoplaría la búsqueda léxica a la disponibilidad del modelo
    y dificultaría razonar sobre la fusión de resultados.
    """
    public_api = {
        name for name in dir(BgeM3EmbeddingService) if not name.startswith("_")
    }
    forbidden = {"sparse", "lexical", "bm25", "term_weights"}
    leaked = {name for name in public_api if any(token in name.lower() for token in forbidden)}
    assert not leaked, f"API de recuperación léxica expuesta por el modelo: {leaked}"

    source = Path(embeddings_module.__file__).read_text(encoding="utf-8")
    assert "return_sparse" not in source
    assert "lexical_weight" not in source


@pytest.mark.robustness
def test_no_separate_embedding_service_declared() -> None:
    """El modelo es una librería local, no un servicio de red independiente.

    Verificación estructural de la orquestación: no debe declararse un servicio
    de embeddings ni infraestructura de apoyo que el sistema no necesita.
    """
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    if not compose_path.exists():
        pytest.skip("docker-compose.yml no presente en esta etapa")

    text = compose_path.read_text(encoding="utf-8").lower()
    forbidden_services = (
        "embeddings:",
        "embedding-service",
        "redis:",
        "rabbitmq:",
        "elasticsearch:",
        "milvus:",
        "qdrant:",
        "weaviate:",
    )
    for forbidden in forbidden_services:
        assert forbidden not in text, f"Servicio no previsto detectado: {forbidden}"
