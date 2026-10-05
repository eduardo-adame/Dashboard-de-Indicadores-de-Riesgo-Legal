"""Pruebas de fusión, estados y frontera de autorización de RETRIEVAL."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.rag.evidence import classify_evidence, fuse_candidates
from app.rag.models import (
    CitationMetadata, EvidenceState, RetrievalContext, RetrievedCandidate,
    RetrievalError,
)
from app.rag.retrieval import RetrievalService
from app.rag.retrieval_repository import RetrievalRepository
from app.rag.retrieval_repository import _vector_literal
from app.security.models import AuthenticatedPrincipal, AuthorizationError, AuthorizedDocumentScope


def _candidate(rank: int, *, complete: bool = True, fragment_id=None) -> RetrievedCandidate:
    identity = fragment_id or uuid4()
    citation = CitationMetadata(
        document_id=str(uuid4()), document_name="Contrato de prueba", document_type="PDF",
        document_date=date(2026, 1, 1) if complete else None, fragment_id=identity,
    )
    return RetrievedCandidate(identity, "Texto sintético", citation, rank, 1.0)


def test_rrf_exact_oracle_deduplicates_and_preserves_original_ranks() -> None:
    shared = _candidate(2)
    vector = (_candidate(1), shared, _candidate(3))
    lexical = (RetrievedCandidate(shared.fragment_id, shared.fragment_text, shared.citation, 1, 2.0), _candidate(2))
    result = fuse_candidates(vector, lexical)
    oracle = sorted(
        [
            (vector[0].fragment_id, Decimal(1) / Decimal(61)),
            (shared.fragment_id, Decimal(1) / Decimal(62) + Decimal(1) / Decimal(61)),
            (vector[2].fragment_id, Decimal(1) / Decimal(63)),
            (lexical[1].fragment_id, Decimal(1) / Decimal(62)),
        ],
        key=lambda pair: -pair[1],
    )
    assert [(item.fragment_id, item.rrf_score) for item in result] == oracle
    assert result[0].vector_rank == 2
    assert result[0].bm25_rank == 1


def test_rrf_ties_use_original_rank_then_fragment_id() -> None:
    first = _candidate(1, fragment_id=__import__("uuid").UUID(int=2))
    second = _candidate(1, fragment_id=__import__("uuid").UUID(int=1))
    result = fuse_candidates((first, second), ())
    assert [item.fragment_id for item in result] == [second.fragment_id, first.fragment_id]
    assert len(result) == 2


def test_evidence_states_require_top3_and_complete_citation() -> None:
    top = _candidate(1)
    missing_date = _candidate(2, complete=False)
    assert classify_evidence((top,), (), fuse_candidates((top,), ())) is EvidenceState.EVIDENCIA_SUFICIENTE
    assert classify_evidence((missing_date,), (), fuse_candidates((missing_date,), ())) is EvidenceState.EVIDENCIA_INSUFICIENTE
    assert classify_evidence((), (), ()) is EvidenceState.SIN_EVIDENCIA


def test_top3_candidate_outside_final_five_does_not_prove_sufficiency() -> None:
    complete_top = _candidate(1)
    shared = tuple(_candidate(index, complete=False) for index in range(2, 7))
    vector = (complete_top,) + shared
    lexical = tuple(
        RetrievedCandidate(item.fragment_id, item.fragment_text, item.citation, index, 1.0)
        for index, item in enumerate(shared, 1)
    )
    final = fuse_candidates(vector, lexical)
    assert len(final) == 5
    assert complete_top.fragment_id not in {item.fragment_id for item in final}
    assert classify_evidence(vector, lexical, final) is EvidenceState.EVIDENCIA_INSUFICIENTE


class _Connection:
    autocommit = True

    def cursor(self, *args, **kwargs):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def execute(self, _sql): return None
            def fetchone(self): return ("read committed",)
        return Cursor()


class _Encoder:
    def encode_query(self, _query):
        return [1.0] * 1024


class _Security:
    def __init__(self, scopes):
        self.scopes = iter(scopes)
        self.calls = 0

    def authorized_document_scope(self, _connection, _principal):
        self.calls += 1
        value = next(self.scopes)
        if isinstance(value, Exception):
            raise value
        return value


class _Repository:
    def __init__(self):
        self.vector_calls = 0
        self.bm25_calls = 0

    def vector_top10(self, _connection, _scope, _vector):
        self.vector_calls += 1
        return (_candidate(1),)

    def bm25_top10(self, _connection, _scope, _terms):
        self.bm25_calls += 1
        return ()

    def final_fragments_still_authorized(self, _connection, _scope, _fragment_ids):
        return True


def _context(reference_id=None):
    principal = AuthenticatedPrincipal(uuid4(), uuid4(), "tester", 1, frozenset({"TI"}), frozenset({"document.query"}))
    return RetrievalContext(principal, "consulta", uuid4(), uuid4(), reference_id)


def _scope(families=frozenset({"LITIGIOS"})):
    return AuthorizedDocumentScope(uuid4(), frozenset({"TI"}), families)


def test_security_revalidation_discards_results_after_revocation() -> None:
    scope = _scope()
    repository = _Repository()
    service = RetrievalService(_Security((scope, scope, AuthorizationError("Acceso no autorizado"))), repository, _Encoder())
    with pytest.raises(AuthorizationError, match="Acceso no autorizado"):
        service.retrieve(_Connection(), _context())
    assert repository.vector_calls == 1
    assert repository.bm25_calls == 1


def test_changed_scope_discards_results_without_returning_fragments() -> None:
    scope = _scope()
    service = RetrievalService(_Security((scope, scope, _scope(frozenset()))), _Repository(), _Encoder())
    with pytest.raises(AuthorizationError):
        service.retrieve(_Connection(), _context())


def test_denial_precedes_ranking_and_inference() -> None:
    class NoInference:
        def encode_query(self, _query):
            raise AssertionError("inference before authorization")

    repository = _Repository()
    service = RetrievalService(_Security((AuthorizationError("Acceso no autorizado"),)), repository, NoInference())
    with pytest.raises(AuthorizationError):
        service.retrieve(_Connection(), _context())
    assert repository.vector_calls == repository.bm25_calls == 0


def test_rf048_reference_does_not_change_ranking_or_state() -> None:
    scope = _scope()
    first = RetrievalService(_Security((scope, scope, scope, scope)), _Repository(), _Encoder()).retrieve(_Connection(), _context())
    second = RetrievalService(_Security((scope, scope, scope, scope)), _Repository(), _Encoder()).retrieve(_Connection(), _context(uuid4()))
    assert first.state == second.state
    assert len(first.fragments) == len(second.fragments) == 1
    assert first.context_reference_id is None
    assert second.context_reference_id is not None


def test_non_autocommit_connection_fails_closed() -> None:
    connection = _Connection()
    connection.autocommit = False
    with pytest.raises(RetrievalError):
        RetrievalService(_Security(()), _Repository(), _Encoder()).retrieve(connection, _context())


def test_bm25_empty_normalized_query_never_executes_sql() -> None:
    repository = RetrievalRepository()
    assert repository.bm25_top10(None, _scope(), ()) == ()


def test_zero_vector_is_rejected_before_sql() -> None:
    with pytest.raises(RetrievalError):
        _vector_literal([0.0] * 1024)

    class ZeroEncoder:
        def encode_query(self, _query):
            return [0.0] * 1024

    scope = _scope()
    repository = _Repository()
    with pytest.raises(RetrievalError):
        RetrievalService(_Security((scope,)), repository, ZeroEncoder()).retrieve(_Connection(), _context())
    assert repository.vector_calls == 0


def test_embedding_failure_is_safe_and_precedes_sql() -> None:
    class FailingEncoder:
        def encode_query(self, _query):
            raise RuntimeError("private model path")

    scope = _scope()
    repository = _Repository()
    with pytest.raises(RetrievalError, match="Consulta vectorial no disponible") as error:
        RetrievalService(_Security((scope,)), repository, FailingEncoder()).retrieve(_Connection(), _context())
    assert "private" not in str(error.value)
    assert repository.vector_calls == 0


def test_revocation_of_candidate_outside_final_five_discards_whole_result() -> None:
    class SixCandidates(_Repository):
        def vector_top10(self, _connection, _scope, _vector):
            return tuple(_candidate(index) for index in range(1, 7))

        def final_fragments_still_authorized(self, _connection, _scope, fragment_ids):
            assert len(fragment_ids) == 6
            return False

    scope = _scope()
    with pytest.raises(AuthorizationError):
        RetrievalService(_Security((scope, scope, scope)), SixCandidates(), _Encoder()).retrieve(
            _Connection(), _context()
        )


def test_scope_revoked_during_final_corpus_check_discards_result() -> None:
    scope = _scope()

    class RevokingRepository(_Repository):
        def final_fragments_still_authorized(self, _connection, _scope, _fragment_ids):
            return True

    security = _Security((scope, scope, scope, _scope(frozenset())))
    with pytest.raises(AuthorizationError, match="Acceso no autorizado"):
        RetrievalService(security, RevokingRepository(), _Encoder()).retrieve(_Connection(), _context())
    assert security.calls == 4


@pytest.mark.parametrize("bad_vector", [None, ["secret path"], (value for value in ()), 3])
def test_malformed_encoder_output_is_safe(bad_vector) -> None:
    class BadEncoder:
        def encode_query(self, _query):
            return bad_vector

    scope = _scope()
    repository = _Repository()
    with pytest.raises(RetrievalError, match="Consulta vectorial no disponible") as error:
        RetrievalService(_Security((scope,)), repository, BadEncoder()).retrieve(_Connection(), _context())
    assert "secret" not in str(error.value)
    assert repository.vector_calls == 0
    with pytest.raises(RetrievalError):
        _vector_literal(bad_vector)
