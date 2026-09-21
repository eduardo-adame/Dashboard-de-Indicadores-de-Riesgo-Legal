"""Pruebas de la frontera de acceso al almacenamiento de objetos."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.storage import (
    StorageAbstraction,
    StorageNotFoundError,
    StorageTraversalError,
    validate_locator,
)


@pytest.fixture()
def storage(tmp_path: Path) -> StorageAbstraction:
    root = tmp_path / "objects"
    root.mkdir()
    return StorageAbstraction(root)


def _write_object(storage: StorageAbstraction, locator: str, content: bytes) -> None:
    path = storage.root / locator
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_valid_locator_returns_readable_stream(storage: StorageAbstraction) -> None:
    _write_object(storage, "objects/ab/abc123-doc.csv", b"id,amount\n1,20\n")
    with storage.open_stream("objects/ab/abc123-doc.csv") as stream:
        assert stream.read() == b"id,amount\n1,20\n"


def test_nonexistent_object_raises_not_found(storage: StorageAbstraction) -> None:
    with pytest.raises(StorageNotFoundError):
        with storage.open_stream("objects/ab/missing.csv"):
            pass


def test_traversal_with_dotdot_is_rejected(storage: StorageAbstraction) -> None:
    with pytest.raises(StorageTraversalError):
        validate_locator("../etc/passwd")
    with pytest.raises(StorageTraversalError):
        storage.resolve("objects/../../etc/passwd")


def test_absolute_path_is_rejected() -> None:
    with pytest.raises(StorageTraversalError):
        validate_locator("/etc/passwd")
    with pytest.raises(StorageTraversalError):
        validate_locator("C:/Windows/system32")


def test_empty_locator_is_rejected() -> None:
    with pytest.raises(StorageTraversalError):
        validate_locator("")
    with pytest.raises(StorageTraversalError):
        validate_locator("   ")


def test_stream_is_closed_after_context(storage: StorageAbstraction) -> None:
    _write_object(storage, "objects/ab/abc-doc.csv", b"data")
    with storage.open_stream("objects/ab/abc-doc.csv") as stream:
        assert stream.read() == b"data"
    assert stream.closed is True


def test_resolved_path_stays_within_root(storage: StorageAbstraction) -> None:
    _write_object(storage, "objects/ab/abc-doc.csv", b"data")
    resolved = storage.resolve("objects/ab/abc-doc.csv")
    assert resolved.is_relative_to(storage.root)


def test_locator_with_backslashes_is_normalized(storage: StorageAbstraction) -> None:
    _write_object(storage, "objects/ab/abc-doc.csv", b"data")
    assert storage.exists("objects\\ab\\abc-doc.csv")


def test_real_locator_format_is_supported(storage: StorageAbstraction) -> None:
    # Formato persistido por la ingesta: objects/{hash_prefix}/{uuid}-{name}
    locator = "objects/3e/ac9d052c-4f25-4c8a-9d2b-1f0e7a6b5c4d-e2e_contracts.csv"
    _write_object(storage, locator, b"id\n1")
    with storage.open_stream(locator) as stream:
        assert stream.read() == b"id\n1"


def test_locator_resolving_outside_root_is_rejected(storage: StorageAbstraction) -> None:
    with pytest.raises(StorageTraversalError):
        storage.resolve("objects/../../outside.csv")


def test_directory_locator_is_not_a_streamable_object(storage: StorageAbstraction) -> None:
    (storage.root / "objects" / "ab").mkdir(parents=True)
    with pytest.raises(StorageNotFoundError):
        with storage.open_stream("objects/ab"):
            pass
