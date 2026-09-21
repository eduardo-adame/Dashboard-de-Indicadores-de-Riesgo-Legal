"""Fragmentación estructural de texto consolidado y activación del corpus."""
from app.corpus.chunker import Chunk, ChunkingConfig, chunk_text
from app.corpus.tokenizer import BgeM3Tokenizer, TokenOffset, TokenizerUnavailableError

__all__ = (
    "BgeM3Tokenizer",
    "Chunk",
    "ChunkingConfig",
    "TokenOffset",
    "TokenizerUnavailableError",
    "chunk_text",
)
