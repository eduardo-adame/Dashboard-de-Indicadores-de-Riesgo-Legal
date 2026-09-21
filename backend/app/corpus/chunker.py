"""Fragmentación estructural del texto consolidado.

Los fragmentos conservan el texto original: el tokenizador solo aporta conteo y
desplazamientos de caracteres. Se mantienen los metadatos de página y de sección
cuando están disponibles, sin normalizar reglas de negocio.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.corpus.tokenizer import Tokenizer

# (page_number, start_char, end_char)
PageMapping = tuple[int, int, int]
# (start_char, end_char, section_label)
SectionMapping = tuple[int, int, str]


@dataclass(frozen=True)
class ChunkingConfig:
    max_tokens: int = 512
    overlap: int = 50

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError("max_tokens debe ser positivo")
        if self.overlap < 0:
            raise ValueError("overlap no puede ser negativo")
        if self.overlap >= self.max_tokens:
            raise ValueError("overlap debe ser menor que max_tokens")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    content: str
    token_count: int
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None


def _page_range(
    char_start: int,
    char_end: int,
    page_mapping: Sequence[PageMapping],
) -> tuple[int | None, int | None]:
    pages = [
        page_number
        for page_number, page_start, page_end in page_mapping
        if char_start < page_end and char_end > page_start
    ]
    if not pages:
        return None, None
    return min(pages), max(pages)


def _section_for(char_start: int, section_mapping: Sequence[SectionMapping]) -> str | None:
    for start, end, label in section_mapping:
        if start <= char_start < end:
            return label
    return None


def chunk_text(
    text: str,
    tokenizer: Tokenizer,
    config: ChunkingConfig | None = None,
    page_mapping: Sequence[PageMapping] = (),
    section_mapping: Sequence[SectionMapping] = (),
) -> list[Chunk]:
    """Fragmenta ``text`` en unidades de hasta ``max_tokens`` con solapamiento."""
    config = config or ChunkingConfig()
    offsets = tokenizer.tokenize_with_offsets(text)
    if not offsets:
        return []

    stride = config.max_tokens - config.overlap
    chunks: list[Chunk] = []
    ordinal = 0
    start = 0
    total = len(offsets)
    while start < total:
        end = min(start + config.max_tokens, total)
        char_start = offsets[start][0]
        char_end = offsets[end - 1][1]
        page_start, page_end = _page_range(char_start, char_end, page_mapping)
        chunks.append(
            Chunk(
                ordinal=ordinal,
                content=text[char_start:char_end],
                token_count=end - start,
                page_start=page_start,
                page_end=page_end,
                section=_section_for(char_start, section_mapping),
            )
        )
        ordinal += 1
        if end >= total:
            break
        start += stride
    return chunks
