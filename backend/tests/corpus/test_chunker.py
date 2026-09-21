"""Pruebas del fragmentador estructural del corpus."""
from __future__ import annotations

from app.corpus.chunker import ChunkingConfig, chunk_text
from app.corpus.tokenizer import FastTokenizer


def _text_with_tokens(count: int) -> str:
    return " ".join(f"t{i}" for i in range(count))


def test_chunks_respect_max_tokens_and_preserve_original_text() -> None:
    tokenizer = FastTokenizer()
    text = _text_with_tokens(120)
    chunks = chunk_text(text, tokenizer, ChunkingConfig(max_tokens=50, overlap=10))
    assert len(chunks) >= 2
    assert all(chunk.token_count <= 50 for chunk in chunks)
    # El contenido de cada fragmento es un subtexto literal del original.
    for chunk in chunks:
        assert chunk.content in text


def test_chunk_overlap_repeats_boundary_tokens() -> None:
    tokenizer = FastTokenizer()
    text = _text_with_tokens(30)
    chunks = chunk_text(text, tokenizer, ChunkingConfig(max_tokens=10, overlap=3))
    assert chunks[0].content.split()[-3:] == chunks[1].content.split()[:3]


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("", FastTokenizer(), ChunkingConfig()) == []


def test_page_mapping_is_preserved() -> None:
    tokenizer = FastTokenizer()
    text = "alpha beta gamma delta"
    # Página 1 = [0, 11), página 2 = [11, 22)
    page_mapping = ((1, 0, 11), (2, 11, len(text)))
    chunks = chunk_text(text, tokenizer, ChunkingConfig(max_tokens=4, overlap=0), page_mapping=page_mapping)
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2


def test_section_mapping_is_preserved() -> None:
    tokenizer = FastTokenizer()
    text = "clause one clause two"
    section_mapping = ((0, 15, "Artículo 1"),)
    chunks = chunk_text(text, tokenizer, ChunkingConfig(max_tokens=4, overlap=0), section_mapping=section_mapping)
    assert chunks[0].section == "Artículo 1"


def test_overlap_must_be_lower_than_max_tokens() -> None:
    import pytest

    with pytest.raises(ValueError):
        ChunkingConfig(max_tokens=10, overlap=10)
