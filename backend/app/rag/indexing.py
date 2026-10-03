"""Indexación documental determinista y certificable para RAG."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
from typing import Protocol, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.corpus.tokenizer import Tokenizer
from app.embeddings import get_embedding_service
from app.rag.lexical import LexicalTermFrequency, lexical_term_frequencies


INDEX_CONTRACT_VERSION = "bge-m3:d1024:c512:o50:lex-nfkc-casefold-alnum-v1"
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIMENSIONS = 1024
MAX_CHUNK_TOKENS = 512
OVERLAP_TOKENS = 50
_FRAGMENT_DOMAIN = "riesgo-legal:RAG_INDEX_FRAGMENT:v1"
_STRUCTURAL_HEADING = re.compile(
    r"(?im)^(?:secci[oó]n|art[ií]culo|cl[aá]usula)\b[^\r\n]*"
)

PageMapping = tuple[int, int, int]


class IndexingError(RuntimeError):
    """Error controlado durante la indexación documental."""


class IndexingInvariantError(IndexingError):
    """Un retry reutilizó una identidad con contenido incompatible."""


class EmbeddingEncoder(Protocol):
    def encode_documents(
        self,
        texts: Sequence[str],
        normalize_embeddings: bool = False,
    ) -> list[list[float]]: ...


@dataclass(frozen=True)
class IndexChunk:
    fragment_id: UUID
    ordinal: int
    content: str
    token_count: int
    page_start: int | None
    page_end: int | None
    section: str | None
    clause: str | None
    embedding: tuple[float, ...]
    terms: tuple[LexicalTermFrequency, ...]


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    content: str
    token_count: int
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    clause: str | None = None


@dataclass(frozen=True)
class IndexingResult:
    document_version_id: UUID
    fragment_ids: tuple[UUID, ...]
    source_text_sha256: bytes
    reused_certificate: bool


def canonical_fragment_name(document_version_id: UUID, ordinal: int) -> str:
    """Serializa la identidad estable de un fragmento."""
    if ordinal < 0:
        raise ValueError("el ordinal no puede ser negativo")
    return (
        f"{_FRAGMENT_DOMAIN}:{INDEX_CONTRACT_VERSION}:"
        f"{str(document_version_id).lower()}:{ordinal}"
    )


def derive_fragment_id(document_version_id: UUID, ordinal: int) -> UUID:
    return uuid5(NAMESPACE_URL, canonical_fragment_name(document_version_id, ordinal))


def source_text_sha256(text: str) -> bytes:
    if not isinstance(text, str):
        raise TypeError("el texto consolidado debe ser str")
    return hashlib.sha256(text.encode("utf-8")).digest()


def _page_range(
    char_start: int,
    char_end: int,
    page_mapping: Sequence[PageMapping],
) -> tuple[int | None, int | None]:
    pages = [
        page
        for page, start, end in page_mapping
        if char_start < end and char_end > start
    ]
    return (min(pages), max(pages)) if pages else (None, None)


def _structural_metadata(
    text: str,
    char_start: int,
    char_end: int,
) -> tuple[str | None, str | None]:
    """Conserva la etiqueta estructural explícita aplicable al fragmento."""
    previous = None
    contained = None
    for match in _STRUCTURAL_HEADING.finditer(text):
        if match.start() >= char_end:
            break
        if match.start() >= char_start and contained is None:
            contained = match
        elif match.start() < char_start:
            previous = match
    selected = contained or previous
    if selected is None:
        return None, None
    label = selected.group(0).strip()
    if label.casefold().startswith("cláusula"):
        return None, label
    return label, None


def _preferred_end(
    text: str,
    offsets: Sequence[tuple[int, int]],
    start: int,
    hard_end: int,
) -> int:
    """Elige el último límite estructural disponible dentro de la ventana."""
    if hard_end >= len(offsets):
        return hard_end
    minimum = min(hard_end, start + OVERLAP_TOKENS + 1)
    candidates = list(range(minimum, hard_end + 1))

    for end in reversed(candidates):
        next_start = offsets[end][0] if end < len(offsets) else len(text)
        tail = text[offsets[end - 1][1]:next_start]
        upcoming = text[next_start:]
        if "\n" in tail and _STRUCTURAL_HEADING.match(upcoming):
            return end
    for end in reversed(candidates):
        next_start = offsets[end][0] if end < len(offsets) else len(text)
        if "\n\n" in text[offsets[end - 1][1]:next_start]:
            return end
    for end in reversed(candidates):
        char_end = offsets[end - 1][1]
        if text[:char_end].rstrip().endswith((".", "?", "!")):
            return end
    return hard_end


def structural_chunk_text(
    text: str,
    tokenizer: Tokenizer,
    *,
    page_mapping: Sequence[PageMapping] = (),
) -> tuple[ChunkDraft, ...]:
    """Fragmenta con prioridad estructural y límites reales del tokenizador."""
    offsets = tokenizer.tokenize_with_offsets(text)
    if not offsets:
        return ()

    chunks: list[ChunkDraft] = []
    start = 0
    ordinal = 0
    while start < len(offsets):
        hard_end = min(start + MAX_CHUNK_TOKENS, len(offsets))
        end = _preferred_end(text, offsets, start, hard_end)
        if end <= start:
            end = hard_end
        char_start = offsets[start][0]
        char_end = offsets[end - 1][1]
        page_start, page_end = _page_range(char_start, char_end, page_mapping)
        section, clause = _structural_metadata(text, char_start, char_end)
        chunks.append(
            ChunkDraft(
                ordinal=ordinal,
                content=text[char_start:char_end],
                token_count=end - start,
                page_start=page_start,
                page_end=page_end,
                section=section,
                clause=clause,
            )
        )
        if end >= len(offsets):
            break
        next_start = max(start + 1, end - OVERLAP_TOKENS)
        start = next_start
        ordinal += 1
    return tuple(chunks)


class IndexingService:
    """Calcula fuera de transacción y delega la certificación al repositorio."""

    def __init__(self, repository, encoder: EmbeddingEncoder | None = None) -> None:
        self.repository = repository
        self.encoder = encoder or get_embedding_service()

    def index_version(
        self,
        *,
        document_version_id: UUID,
        consolidated_text: str,
        tokenizer: Tokenizer,
        page_mapping: Sequence[PageMapping] = (),
    ) -> IndexingResult:
        digest = source_text_sha256(consolidated_text)
        drafts = structural_chunk_text(
            consolidated_text,
            tokenizer,
            page_mapping=page_mapping,
        )
        if not drafts:
            raise IndexingError("el texto consolidado no produjo fragmentos")

        vectors = self.encoder.encode_documents([draft.content for draft in drafts])
        if len(vectors) != len(drafts):
            raise IndexingError("el encoder no devolvió un vector por fragmento")

        chunks: list[IndexChunk] = []
        for draft, vector in zip(drafts, vectors, strict=True):
            if len(vector) != EMBEDDING_DIMENSIONS or not all(
                math.isfinite(float(component)) for component in vector
            ):
                raise IndexingError("embedding incompatible con vector(1024)")
            chunks.append(
                IndexChunk(
                    fragment_id=derive_fragment_id(document_version_id, draft.ordinal),
                    ordinal=draft.ordinal,
                    content=draft.content,
                    token_count=draft.token_count,
                    page_start=draft.page_start,
                    page_end=draft.page_end,
                    section=draft.section,
                    clause=draft.clause,
                    embedding=tuple(float(component) for component in vector),
                    terms=lexical_term_frequencies(draft.content),
                )
            )

        fragment_ids, reused_certificate = self.repository.persist_certified_index(
            document_version_id=document_version_id,
            consolidated_text=consolidated_text,
            source_text_sha256=digest,
            chunks=tuple(chunks),
        )
        return IndexingResult(
            document_version_id,
            fragment_ids,
            digest,
            reused_certificate,
        )
