"""Pruebas unitarias del contrato de indexación RAG."""
from __future__ import annotations

import math
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from app.corpus.tokenizer import FastTokenizer
from app.embeddings import get_embedding_service, reset_embedding_service
from app.rag.indexing import (
    INDEX_CONTRACT_VERSION,
    IndexingError,
    IndexingService,
    canonical_fragment_name,
    derive_fragment_id,
    structural_chunk_text,
)
from app.rag.lexical import (
    LEXICAL_CONTRACT_VERSION,
    lexical_term_frequencies,
)


class _Encoder:
    def encode_documents(self, texts, normalize_embeddings=False):
        return [[float(index + 1)] * 1024 for index, _ in enumerate(texts)]


class _Repository:
    def __init__(self):
        self.persisted = None
        self.existing = None

    def persist_certified_index(self, **kwargs):
        self.persisted = kwargs
        return (
            tuple(chunk.fragment_id for chunk in kwargs["chunks"]),
            self.existing is not None,
        )


def _long_text(total: int = 700) -> str:
    return " ".join(f"token-{index}" for index in range(total))


def test_lexical_contract_nfkc_casefold_alnum() -> None:
    terms = lexical_term_frequencies("ＡBC áBC, Niño_42 niño-42")
    assert LEXICAL_CONTRACT_VERSION == "lex-nfkc-casefold-alnum-v1"
    assert [(item.normalized_lexeme, item.term_frequency) for item in terms] == [
        ("42", 2),
        ("abc", 1),
        ("niño", 2),
        ("ábc", 1),
    ]


def test_lexical_order_is_canonical_and_zero_lexeme_is_empty() -> None:
    assert lexical_term_frequencies("z Z a") == (
        lexical_term_frequencies("a")[0],
        lexical_term_frequencies("z z")[0],
    )
    assert lexical_term_frequencies("— _ !!!") == ()


def test_lexical_contract_rejects_unknown_version() -> None:
    with pytest.raises(ValueError):
        lexical_term_frequencies("texto", contract_version="otro")


def test_fragment_uuidv5_uses_canonical_serialization() -> None:
    version_id = UUID("11111111-2222-3333-4444-555555555555")
    name = (
        "riesgo-legal:RAG_INDEX_FRAGMENT:v1:"
        f"{INDEX_CONTRACT_VERSION}:{str(version_id).lower()}:7"
    )
    assert canonical_fragment_name(version_id, 7) == name
    assert derive_fragment_id(version_id, 7) == uuid5(NAMESPACE_URL, name)


def test_same_version_and_ordinal_have_stable_fragment_id() -> None:
    version_id = uuid4()
    assert derive_fragment_id(version_id, 0) == derive_fragment_id(version_id, 0)
    assert derive_fragment_id(version_id, 0) != derive_fragment_id(version_id, 1)


def test_chunking_is_deterministic_and_preserves_512_50_contract() -> None:
    text = _long_text()
    tokenizer = FastTokenizer()
    first = structural_chunk_text(text, tokenizer)
    second = structural_chunk_text(text, tokenizer)
    assert first == second
    assert [chunk.token_count for chunk in first] == [512, 238]
    first_tokens = first[0].content.split()
    second_tokens = first[1].content.split()
    assert first_tokens[-50:] == second_tokens[:50]


def test_structural_chunking_prefers_paragraph_boundary() -> None:
    first = " ".join(f"a{index}" for index in range(480))
    second = " ".join(f"b{index}" for index in range(100))
    chunks = structural_chunk_text(f"{first}\n\n{second}", FastTokenizer())
    assert chunks[0].token_count == 480
    assert chunks[0].content.endswith("a479")


def test_structural_chunking_prefers_heading_boundary() -> None:
    first = " ".join(f"a{index}" for index in range(480))
    second = " ".join(f"b{index}" for index in range(100))
    chunks = structural_chunk_text(
        f"{first}\nCláusula Décima\n{second}", FastTokenizer()
    )
    assert chunks[0].token_count == 480
    assert chunks[0].content.endswith("a479")
    assert "Cláusula Décima" in chunks[1].content
    assert chunks[1].clause == "Cláusula Décima"


def test_structural_chunking_prefers_sentence_before_hard_split() -> None:
    first = " ".join(f"a{index}" for index in range(480)) + "."
    second = " ".join(f"b{index}" for index in range(100))
    chunks = structural_chunk_text(f"{first} {second}", FastTokenizer())
    assert chunks[0].token_count == 480
    assert chunks[0].content.endswith("a479.")


def test_structural_chunking_uses_hard_limit_without_boundary() -> None:
    chunks = structural_chunk_text(_long_text(600), FastTokenizer())
    assert chunks[0].token_count == 512
    assert chunks[1].token_count == 138


def test_page_metadata_is_preserved_without_invention() -> None:
    text = "primera pagina segunda pagina"
    chunks = structural_chunk_text(
        text,
        FastTokenizer(),
        page_mapping=((1, 0, 14), (2, 15, len(text))),
    )
    assert chunks[0].page_start == 1
    assert chunks[0].page_end == 2
    assert chunks[0].section is None
    assert chunks[0].clause is None


def test_explicit_section_and_clause_metadata_are_preserved() -> None:
    section = structural_chunk_text(
        "Artículo 7\nContenido contractual.", FastTokenizer()
    )
    clause = structural_chunk_text(
        "Cláusula Décima\nObligación exigible.", FastTokenizer()
    )
    assert section[0].section == "Artículo 7"
    assert section[0].clause is None
    assert clause[0].section is None
    assert clause[0].clause == "Cláusula Décima"


def test_index_service_persists_embeddings_terms_and_stable_ids() -> None:
    repository = _Repository()
    version_id = uuid4()
    result = IndexingService(repository, _Encoder()).index_version(
        document_version_id=version_id,
        consolidated_text="Contrato ÚNICO 2026",
        tokenizer=FastTokenizer(),
    )
    chunk = repository.persisted["chunks"][0]
    assert result.fragment_ids == (derive_fragment_id(version_id, 0),)
    assert chunk.fragment_id == result.fragment_ids[0]
    assert len(chunk.embedding) == 1024
    assert [term.normalized_lexeme for term in chunk.terms] == [
        "2026", "contrato", "único"
    ]


def test_index_service_reconciles_existing_certificate_after_encoding() -> None:
    repository = _Repository()
    repository.existing = (uuid4(),)
    version_id = uuid4()
    result = IndexingService(repository, _Encoder()).index_version(
        document_version_id=version_id,
        consolidated_text="texto",
        tokenizer=FastTokenizer(),
    )
    assert result.reused_certificate is True
    assert result.fragment_ids == (derive_fragment_id(version_id, 0),)
    assert repository.persisted is not None


def test_invalid_embedding_fails_before_persistence() -> None:
    repository = _Repository()

    class _InvalidEncoder:
        def encode_documents(self, texts, normalize_embeddings=False):
            return [[math.nan] * 1024]

    with pytest.raises(IndexingError):
        IndexingService(repository, _InvalidEncoder()).index_version(
            document_version_id=uuid4(),
            consolidated_text="texto",
            tokenizer=FastTokenizer(),
        )
    assert repository.persisted is None


@pytest.mark.requires_model
@pytest.mark.contract
def test_real_bge_m3_smoke_returns_1024_dimensions() -> None:
    """El smoke obligatorio usa tokenizador y pesos reales, sin sustitutos."""
    from app.corpus.tokenizer import BgeM3Tokenizer

    reset_embedding_service()
    tokenizer = BgeM3Tokenizer()
    assert tokenizer.count_tokens("prueba contractual de indexación") > 0
    service = get_embedding_service()
    vectors = service.encode_documents(["prueba contractual de indexación"])
    assert service.model_id == "BAAI/bge-m3"
    assert len(vectors) == 1
    assert len(vectors[0]) == 1024
    assert all(math.isfinite(value) for value in vectors[0])
