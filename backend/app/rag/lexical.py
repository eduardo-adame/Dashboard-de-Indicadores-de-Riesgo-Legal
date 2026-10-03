"""Contrato léxico compartido para indexación y recuperación RAG."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Final
import unicodedata


LEXICAL_CONTRACT_VERSION: Final[str] = "lex-nfkc-casefold-alnum-v1"


@dataclass(frozen=True)
class LexicalTermFrequency:
    """Frecuencia de un lexema normalizado, ordenable de forma determinista."""

    normalized_lexeme: str
    term_frequency: int


def lexical_term_frequencies(
    text: str,
    *,
    contract_version: str = LEXICAL_CONTRACT_VERSION,
) -> tuple[LexicalTermFrequency, ...]:
    """Normaliza y cuenta secuencias alfanuméricas Unicode.

    El texto se transforma con NFKC y ``casefold``. No se aplican stemming,
    stopwords, transliteración ni términos centinela.
    """
    if not isinstance(text, str):
        raise TypeError("el texto léxico debe ser str")
    if contract_version != LEXICAL_CONTRACT_VERSION:
        raise ValueError("versión de contrato léxico no soportada")

    normalized = unicodedata.normalize("NFKC", text).casefold()
    lexemes: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            lexemes.append("".join(current))
            current.clear()
    if current:
        lexemes.append("".join(current))

    counts = Counter(lexemes)
    return tuple(
        LexicalTermFrequency(lexeme, counts[lexeme])
        for lexeme in sorted(counts)
    )
