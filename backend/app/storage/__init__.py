"""Frontera de acceso a objetos binarios almacenados en el sistema de archivos."""
from app.storage.abstraction import (
    StorageAbstraction,
    StorageError,
    StorageNotFoundError,
    StorageTraversalError,
    validate_locator,
)

__all__ = (
    "StorageAbstraction",
    "StorageError",
    "StorageNotFoundError",
    "StorageTraversalError",
    "validate_locator",
)
