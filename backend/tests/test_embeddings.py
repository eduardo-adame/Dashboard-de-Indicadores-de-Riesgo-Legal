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
import re
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

# Subruta de la raíz única de caché, relativa a BGE_M3_CACHE_DIR.
CACHE_ROOT_SUBPATH = ("huggingface", "hub")

# Variables que deben apuntar EXACTAMENTE a la raíz única. Un directorio hermano
# distinto provoca una segunda descarga del modelo dentro del mismo volumen.
CACHE_ROOT_VARIABLES = ("HF_HUB_CACHE", "SENTENCE_TRANSFORMERS_HOME", "TRANSFORMERS_CACHE")


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
    counter = {"instances": 0, "encode_calls": 0, "init_kwargs": []}

    class _FakeSentenceTransformer:
        def __init__(self, model_id: str, **kwargs) -> None:
            counter["instances"] += 1
            counter["init_kwargs"].append(dict(kwargs))
            self.model_id = model_id
            self.cache_folder = kwargs.get("cache_folder")

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
    assert os.environ["SENTENCE_TRANSFORMERS_HOME"] == service.effective_cache_root
    assert os.environ["HF_HOME"].startswith(service.cache_dir)


@pytest.mark.robustness
def test_all_cache_variables_converge_on_a_single_root() -> None:
    """Regresión: TODAS las variables de caché apuntan a la misma raíz.

    No basta con no pasar `cache_folder`: la librería cae entonces a
    `SENTENCE_TRANSFORMERS_HOME`, que es el mismo parámetro por otra vía. Si esa
    variable apuntara a otro directorio, el modelo se descargaría dos veces
    dentro del mismo volumen — exactamente la duplicación que se corrige.
    """
    service = get_embedding_service()
    service._configure_cache_env()  # noqa: SLF001 - verificación interna

    root = service.effective_cache_root
    assert root == str(Path(service.cache_dir, *CACHE_ROOT_SUBPATH)), (
        "la raíz efectiva debe derivarse de BGE_M3_CACHE_DIR"
    )

    for var in CACHE_ROOT_VARIABLES:
        assert os.environ[var] == root, (
            f"{var} debe converger en la raíz única {root}, no en {os.environ[var]}"
        )

    # HF_HOME es el directorio padre del hub: la raíz debe quedar dentro de él.
    assert root.startswith(os.environ["HF_HOME"])

    # Ninguna variable debe apuntar a un directorio hermano distinto.
    siblings = {os.environ[v] for v in CACHE_ROOT_VARIABLES}
    assert len(siblings) == 1, f"hay más de una raíz de caché: {siblings}"


def _declared_cache_env() -> dict[str, str]:
    """Variables de caché declaradas en `docker-compose.yml`.

    Se leen del archivo en lugar de asumirlas para poder comprobar la política
    declarativa, que es la que rige antes de que el servicio arranque.
    """
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    if not compose_path.exists():
        pytest.skip("docker-compose.yml no presente en esta etapa")

    text = compose_path.read_text(encoding="utf-8")
    declared: dict[str, str] = {}
    for var in (*CACHE_ROOT_VARIABLES, "BGE_M3_CACHE_DIR", "HF_HOME"):
        match = re.search(rf"^\s*{var}:\s*(\S+)\s*$", text, re.MULTILINE)
        if match:
            declared[var] = match.group(1).strip().strip("\"'")
    return declared


def _declared_root(declared: dict[str, str]) -> str:
    """Raíz de caché declarada, en convención POSIX.

    Las rutas del Compose son rutas de contenedor (POSIX), no rutas del sistema
    de archivos donde se ejecutan las pruebas. Construirlas con `Path` las
    convertiría a separadores de Windows y la comparación no tendría sentido.
    """
    return declared["BGE_M3_CACHE_DIR"].rstrip("/") + "/" + "/".join(CACHE_ROOT_SUBPATH)


@pytest.mark.robustness
def test_compose_declares_a_single_cache_root() -> None:
    """La configuración declarada en Compose no debe introducir una segunda raíz.

    Comprobar solo el valor efectivo en tiempo de ejecución no bastaría: si el
    Compose declara un directorio hermano, la duplicación reaparece en cuanto algo
    deje de sobrescribirlo. La política declarativa debe ser correcta por sí misma.
    """
    declared = _declared_cache_env()
    if "BGE_M3_CACHE_DIR" not in declared:
        pytest.skip("BGE_M3_CACHE_DIR no declarado en el Compose")

    expected_root = _declared_root(declared)

    for var in CACHE_ROOT_VARIABLES:
        assert var in declared, f"{var} debe estar declarado en el Compose"
        assert declared[var] == expected_root, (
            f"{var} declarado como {declared[var]}; debe ser la raíz única {expected_root}"
        )

    # HF_HOME es el directorio padre que contiene la raíz del hub, no la raíz.
    if "HF_HOME" in declared:
        assert expected_root.startswith(declared["HF_HOME"]), (
            "HF_HOME debe ser el directorio padre de la raíz de caché"
        )
        assert declared["HF_HOME"] != expected_root, (
            "HF_HOME no debe confundirse con la raíz del hub"
        )

    # Guarda explícita contra el directorio hermano que causó la duplicación.
    for var, value in declared.items():
        assert "sentence_transformers" not in value, (
            f"{var} apunta a un directorio hermano: {value}"
        )


@pytest.mark.robustness
def test_declared_and_effective_cache_policies_agree() -> None:
    """La política declarada y la efectiva derivan la raíz de la misma forma.

    Ambas deben construir la raíz como `<BGE_M3_CACHE_DIR>/huggingface/hub`. Si
    divergieran, la configuración declarativa y la efectiva dejarían de ser una
    sola política y la duplicación podría reaparecer.
    """
    declared = _declared_cache_env()
    if "BGE_M3_CACHE_DIR" not in declared:
        pytest.skip("BGE_M3_CACHE_DIR no declarado en el Compose")

    # Estructura relativa declarada, en convención POSIX.
    declared_parts = tuple(Path(_declared_root(declared)).parts[-2:])

    service = get_embedding_service()
    effective_root = service.effective_cache_root

    # La raíz efectiva se deriva de BGE_M3_CACHE_DIR con la misma subruta.
    assert os.path.normcase(effective_root) == os.path.normcase(
        os.path.join(service.cache_dir, *CACHE_ROOT_SUBPATH)
    ), "la raíz efectiva debe derivarse de BGE_M3_CACHE_DIR"

    # Y la subruta debe ser idéntica a la declarada.
    effective_parts = tuple(Path(effective_root).parts[-2:])
    assert declared_parts == effective_parts == CACHE_ROOT_SUBPATH, (
        "la subruta de la raíz debe coincidir entre la política declarada y la efectiva"
    )


@pytest.mark.robustness
def test_single_cache_root_is_the_hub_cache() -> None:
    """Existe una sola raíz de caché y es la de `HF_HUB_CACHE`.

    La raíz se deriva de `BGE_M3_CACHE_DIR` y coincide con la variable de entorno
    que la librería consulta, de modo que no hay dos fuentes de verdad.
    """
    service = get_embedding_service()
    service._configure_cache_env()  # noqa: SLF001 - verificación interna

    expected_root = os.path.join(service.cache_dir, "huggingface", "hub")
    assert service.effective_cache_root == expected_root
    assert os.environ["HF_HUB_CACHE"] == expected_root
    assert service.status().cache_root == expected_root


@pytest.mark.robustness
def test_model_is_loaded_without_redundant_cache_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresión: la carga no debe pasar `cache_folder`.

    Ese parámetro prevalece sobre `HF_HUB_CACHE` y provocaría que el modelo se
    descargara dos veces dentro del mismo volumen: una en `<caché>/models--...`
    y otra en `<caché>/huggingface/hub/models--...`.
    """
    counter = _install_fake_sentence_transformers(monkeypatch)
    service = get_embedding_service()
    service.encode_query("consulta")

    assert counter["instances"] == 1
    kwargs = counter["init_kwargs"][0]
    assert "cache_folder" not in kwargs, (
        "no debe pasarse cache_folder: HF_HUB_CACHE es la única fuente de verdad"
    )
    assert kwargs == {}, f"no se esperaban argumentos adicionales: {kwargs}"


@pytest.mark.robustness
def test_no_duplicate_cache_root_is_materialized() -> None:
    """Regresión estructural: no debe existir una segunda raíz de caché.

    La ubicación redundante era `<cache_dir>/models--<org>--<modelo>`, creada por
    el parámetro `cache_folder`. Solo debe existir la raíz gobernada por
    `HF_HUB_CACHE`, que es la que la librería consulta.

    La comprobación es estructural: las subcarpetas de la raíz las crea la propia
    librería al descargar, no este servicio, por lo que aquí solo se verifica que
    no aparezca la ubicación duplicada y que la raíz efectiva quede dentro de la
    caché persistente.
    """
    service = get_embedding_service()
    service._configure_cache_env()  # noqa: SLF001 - verificación interna

    redundant = list(Path(service.cache_dir).glob("models--*"))
    assert not redundant, f"raíz de caché duplicada detectada: {redundant}"

    root = service.effective_cache_root
    assert root.startswith(service.cache_dir), (
        "la raíz efectiva debe quedar dentro de la caché persistente"
    )
    assert root == os.environ["HF_HUB_CACHE"], (
        "la raíz efectiva debe ser exactamente HF_HUB_CACHE"
    )


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
