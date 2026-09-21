"""Acceso controlado a objetos binarios referenciados por un localizador opaco.

El localizador de ``app.stored_object`` es relativo a la raíz de almacenamiento y
nunca se persiste una ruta absoluta del host. Esta frontera:

* valida el localizador antes de tocar el sistema de archivos;
* impide el cruce de directorios (traversal) y las rutas absolutas;
* resuelve el objeto dentro de la raíz de almacenamiento;
* expone un flujo de lectura que el consumidor debe cerrar.

No depende de la lógica interna de ningún módulo productor: interpreta
exactamente el formato de localizador ya persistido.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Tipos de objeto que el almacenamiento reconoce. El localizador siempre usa
# separadores POSIX y es relativo a la raíz.
_FORBIDDEN_SEGMENTS = ("..",)


class StorageError(Exception):
    """Error base de acceso al almacenamiento."""


class StorageTraversalError(StorageError):
    """El localizador intenta salir de la raíz de almacenamiento."""


class StorageNotFoundError(StorageError):
    """El objeto referenciado no existe."""


def validate_locator(locator: str) -> str:
    """Valida y normaliza un localizador opaco.

    Rechaza rutas absolutas, segmentos de escape y localizadores vacíos. No toca
    el sistema de archivos; la comprobación de contención efectiva la realiza
    ``StorageAbstraction`` sobre la ruta resuelta.
    """
    if not locator or not locator.strip():
        raise StorageTraversalError("localizador vacío")

    normalized = locator.replace("\\", "/")
    if normalized.startswith("/"):
        raise StorageTraversalError("no se admiten rutas absolutas")

    # Un prefijo de unidad (``C:``) o cualquier ``:`` indica una ruta de host.
    if ":" in normalized:
        raise StorageTraversalError("no se admiten rutas de host")

    segments = [segment for segment in normalized.split("/") if segment not in ("", ".")]
    if not segments:
        raise StorageTraversalError("localizador sin segmentos")
    for segment in segments:
        if segment in _FORBIDDEN_SEGMENTS:
            raise StorageTraversalError("segmento de escape en el localizador")

    return "/".join(segments)


class StorageAbstraction:
    """Resuelve localizadores contra una raíz de almacenamiento fija."""

    def __init__(self, storage_root: Path) -> None:
        self._root = Path(storage_root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, locator: str) -> Path:
        """Devuelve la ruta absoluta validada de un localizador.

        Aplica ``validate_locator`` y confirma que la ruta resuelta permanece
        dentro de la raíz, incluso frente a enlaces simbólicos.
        """
        relative = validate_locator(locator)
        candidate = (self._root / relative).resolve()
        if candidate != self._root and not candidate.is_relative_to(self._root):
            raise StorageTraversalError("el localizador resuelve fuera de la raíz")
        return candidate

    def exists(self, locator: str) -> bool:
        return self.resolve(locator).is_file()

    @contextmanager
    def open_stream(self, locator: str) -> Iterator[object]:
        """Abre un flujo de lectura binario para el objeto referenciado.

        El flujo se cierra al salir del contexto. Lanza ``StorageNotFoundError``
        si el objeto no existe o no es un archivo regular.
        """
        path = self.resolve(locator)
        if not path.is_file():
            raise StorageNotFoundError("el objeto referenciado no existe")
        with path.open("rb") as stream:
            yield stream
