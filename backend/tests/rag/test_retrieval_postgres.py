"""Evidencia PostgreSQL real de la recuperación documental autorizada."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import math
import os
from pathlib import Path
from time import perf_counter
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config
import psycopg
from psycopg.rows import dict_row, tuple_row
import pytest

from app.corpus.tokenizer import FastTokenizer
from app.rag.index_repository import IndexRepository
from app.rag.indexing import IndexingService
from app.rag.lexical import lexical_term_frequencies
from app.rag.models import EvidenceState, RetrievalContext
from app.rag.retrieval import RetrievalService
from app.rag.retrieval_repository import RetrievalRepository
from app.security.models import AuthenticatedPrincipal, AuthenticationError, AuthorizationError, ROLE_PERMISSIONS
from app.security.repository import SecurityRepository
from app.security.service import SecurityService


BACKEND_ROOT = Path(__file__).resolve().parents[2]


class _Database(dict):
    def __repr__(self) -> str:
        return f"_Database(name={self['name']!r}, credentials='<redacted>')"


class _Encoder:
    def __init__(self, vector: tuple[float, ...] = (0.8, 0.2)) -> None:
        self.vector = vector

    def encode_documents(self, texts, normalize_embeddings=False):
        return [[*self.vector, *([0.0] * 1022)] for _ in texts]

    def encode_query(self, _text, normalize_embeddings=False):
        return [1.0, 0.0, *([0.0] * 1022)]


@pytest.fixture(scope="module")
def database():
    host = os.environ.get("POSTGRES_HOST", "")
    owner = os.environ.get("POSTGRES_USER", "")
    owner_password = os.environ.get("POSTGRES_PASSWORD", "")
    runtime = os.environ.get("RUNTIME_POSTGRES_USER", "")
    runtime_password = os.environ.get("RUNTIME_POSTGRES_PASSWORD", "")
    if not all((host, owner, owner_password, runtime, runtime_password)):
        pytest.fail("Falta configuración protegida para PostgreSQL de pruebas")
    name = f"rag_retrieval_build_test_{uuid4().hex}"
    maintenance = f"host={host} port={os.environ.get('POSTGRES_PORT', '5432')} dbname=postgres user={owner} password={owner_password}"
    owner_dsn = f"host={host} port={os.environ.get('POSTGRES_PORT', '5432')} dbname={name} user={owner} password={owner_password}"
    runtime_dsn = f"host={host} port={os.environ.get('POSTGRES_PORT', '5432')} dbname={name} user={runtime} password={runtime_password}"
    created = False
    try:
        with psycopg.connect(maintenance, autocommit=True) as admin:
            current = admin.execute("SELECT current_database(), current_user").fetchone()
            assert current[0] == "postgres" and current[1] == owner
            assert name.startswith("rag_retrieval_build_test_")
            admin.execute(f'CREATE DATABASE "{name}"')
            created = True
        previous = os.environ.get("POSTGRES_DB")
        try:
            os.environ["POSTGRES_DB"] = name
            command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "0016_rag_operation_lifecycle")
        finally:
            if previous is None:
                os.environ.pop("POSTGRES_DB", None)
            else:
                os.environ["POSTGRES_DB"] = previous
        with psycopg.connect(owner_dsn, autocommit=True) as check:
            assert check.execute("SELECT current_database()").fetchone()[0] == name
            assert check.execute("SELECT version_num FROM public.alembic_version").fetchone()[0] == "0016_rag_operation_lifecycle"
        yield _Database(name=name, owner=owner_dsn, runtime=runtime_dsn, user=runtime)
    finally:
        if created:
            with psycopg.connect(maintenance, autocommit=True) as admin:
                assert name.startswith("rag_retrieval_build_test_")
                active = admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (name,)
                ).fetchone()[0]
                assert active == 0, f"Base desechable preservada por sesiones abiertas: {name}"
                admin.execute(f'DROP DATABASE "{name}"')
                assert admin.execute(
                    "SELECT count(*) FROM pg_database WHERE datname = %s", (name,)
                ).fetchone()[0] == 0


def _principal(database, role="JURIDICO") -> AuthenticatedPrincipal:
    identity, session = uuid4(), uuid4()
    username = f"rag-retrieval-{identity.hex}"
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute(
            "INSERT INTO app.user_account (id, username, display_name, password_hash) VALUES (%s, %s, 'Synthetic', 'hash')",
            (identity, username),
        )
        connection.execute("INSERT INTO app.user_role (user_id, role_id) VALUES (%s, %s)", (identity, role))
        connection.execute(
            "INSERT INTO app.access_session (id, user_id, refresh_token_sha256, authorization_version, state, expires_at) "
            "VALUES (%s, %s, %s, 1, 'ACTIVE', %s)",
            (session, identity, bytes(32), datetime.now(UTC) + timedelta(hours=2)),
        )
    return AuthenticatedPrincipal(identity, session, username, 1, frozenset({role}), ROLE_PERMISSIONS[role])


def _grant(database, role="JURIDICO", family="CONTRATOS_DOCUMENTOS") -> None:
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        SecurityRepository.set_scope_grant(connection, role, family, True)


def _document(database, *, family="CONTRATOS_DOCUMENTOS", text="contrato prueba", vector=(0.8, 0.2), certified=True, document_date=date(2026, 1, 1)):
    file_id, stored_id, version_id = uuid4(), uuid4(), uuid4()
    document_id = str(file_id)
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute(
            "INSERT INTO app.stored_object (id, storage_kind, locator, sha256, mime_type, byte_size, original_name) "
            "VALUES (%s, 'FILESYSTEM', %s, %s, 'application/pdf', 10, 'synthetic.pdf')",
            (stored_id, f"synthetic/{stored_id}", digest),
        )
        connection.execute(
            "INSERT INTO app.ingest_file "
            "(id, stored_object_id, source_family, exchange_format, state, operation_id, correlation_id, "
            "declared_extension, detected_format, format_classification, technical_result, declared_name, "
            "source_locator, source_revision, actor_identifier, content_sha256) "
            "VALUES (%s, %s, %s, 'PDF', 'COMPLETADO', %s, %s, '.pdf', 'PDF', 'SUPPORTED', 'ACCEPTED', "
            "'synthetic.pdf', %s, 1, 'test', %s)",
            (file_id, stored_id, family, uuid4(), uuid4(), f"synthetic/{file_id}", digest),
        )
        connection.execute(
            "INSERT INTO app.document (id_documento, name, document_type, source_family, document_date) "
            "VALUES (%s, 'Synthetic document', 'CONTRATO', %s, %s)",
            (document_id, family, document_date),
        )
        connection.execute(
            "INSERT INTO app.document_version "
            "(id, id_documento, version_number, stored_object_id, processing_state, consolidated_text, "
            "content_sha256, operation_id, correlation_id) "
            "VALUES (%s, %s, 1, %s, 'PROCESANDO', %s, %s, %s, %s)",
            (version_id, document_id, stored_id, text, digest, uuid4(), uuid4()),
        )
    fragment_id = None
    if certified:
        result = IndexingService(IndexRepository(database["owner"]), _Encoder(vector)).index_version(
            document_version_id=version_id, consolidated_text=text, tokenizer=FastTokenizer()
        )
        fragment_id = result.fragment_ids[0]
        with psycopg.connect(database["owner"], autocommit=True) as connection:
            connection.execute("UPDATE app.document SET active_version_id = %s WHERE id_documento = %s", (version_id, document_id))
    return {"document": document_id, "version": version_id, "fragment": fragment_id, "file": file_id, "stored": stored_id}


def _service(database, encoder=None, repository=None):
    return RetrievalService(
        SecurityService(SecurityRepository(database["runtime"])),
        repository or RetrievalRepository(), encoder or _Encoder(),
    )


def _retrieve(database, principal, query="contrato", *, repository=None, context_reference_id=None):
    context = RetrievalContext(principal, query, uuid4(), uuid4(), context_reference_id)
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        return _service(database, repository=repository).retrieve(connection, context)


def test_runtime_tcp_login_and_required_read_access(database) -> None:
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        row = connection.execute(
            "SELECT current_database(), session_user, current_user, "
            "pg_has_role(current_user, 'riesgo_legal_runtime', 'member')"
        ).fetchone()
        assert tuple(row.values()) == (database["name"], database["user"], database["user"], True)
        assert connection.execute("SELECT count(*) FROM app.document_index_certificate").fetchone()["count"] == 0
        privileges = connection.execute(
            "SELECT has_table_privilege(current_user, 'app.document_chunk', 'SELECT') AS chunk_read, "
            "has_table_privilege(current_user, 'app.document_index_certificate', 'SELECT') AS cert_read, "
            "has_table_privilege(current_user, 'app.document_index_certificate', 'UPDATE') AS cert_update, "
            "has_table_privilege(current_user, 'app.document_chunk_index_receipt', 'SELECT') AS receipt_read, "
            "has_table_privilege(current_user, 'app.document_chunk_index_receipt', 'DELETE') AS receipt_delete"
        ).fetchone()
        assert privileges == {
            "chunk_read": True,
            "cert_read": True, "cert_update": False,
            "receipt_read": True, "receipt_delete": False,
        }


@pytest.mark.parametrize("change,expected_error", [
    ("inactive_session", AuthenticationError),
    ("stale_authorization_version", AuthenticationError),
    ("missing_document_query", AuthorizationError),
])
def test_current_security_rejects_before_inference(database, change, expected_error) -> None:
    principal = _principal(database)
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        if change == "inactive_session":
            admin.execute(
                "UPDATE app.access_session SET state = 'INVALIDATED', invalidated_at = CURRENT_TIMESTAMP "
                "WHERE id = %s", (principal.session_id,)
            )
        elif change == "stale_authorization_version":
            admin.execute(
                "UPDATE app.user_account SET authorization_version = authorization_version + 1 WHERE id = %s",
                (principal.account_id,),
            )
        else:
            admin.execute("DELETE FROM app.user_role WHERE user_id = %s", (principal.account_id,))

    class ForbiddenEncoder:
        def encode_query(self, _query):
            pytest.fail("La inferencia no debe preceder a Security")

    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        with pytest.raises(expected_error):
            _service(database, encoder=ForbiddenEncoder()).retrieve(
                connection, RetrievalContext(principal, "consulta", uuid4(), uuid4())
            )


def test_vector_and_bm25_are_authorized_and_exact(database) -> None:
    principal = _principal(database)
    _grant(database)
    allowed = [_document(database, text=f"contrato caso {index}", vector=(0.8 + index * 0.001, 0.2)) for index in range(12)]
    denied = [_document(database, family="CUMPLIMIENTO", text=f"contrato secreto {index}", vector=(1.0, 0.0)) for index in range(3)]
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        scope = _service(database).security.authorized_document_scope(connection, principal)
        vector = RetrievalRepository().vector_top10(connection, scope, _Encoder().encode_query("contrato"))
        lexical = RetrievalRepository().bm25_top10(connection, scope, ("contrato",))
    allowed_ids = {item["fragment"] for item in allowed}
    denied_ids = {item["fragment"] for item in denied}
    assert len(vector) == len(lexical) == 10
    assert {item.fragment_id for item in vector + lexical}.issubset(allowed_ids)
    assert not {item.fragment_id for item in vector + lexical}.intersection(denied_ids)
    exact = sorted(
        allowed,
        key=lambda item: (
            1 - (0.8 + allowed.index(item) * 0.001) / math.hypot(0.8 + allowed.index(item) * 0.001, 0.2),
            str(item["fragment"]),
        ),
    )[:10]
    assert [item.fragment_id for item in vector] == [item["fragment"] for item in exact]
    lexical_oracle = sorted((item["fragment"] for item in allowed), key=str)[:10]
    assert [item.fragment_id for item in lexical] == lexical_oracle
    idf = math.log(1.0 + (12.0 - 12.0 + 0.5) / (12.0 + 0.5))
    assert all(math.isclose(item.score, idf, rel_tol=1e-12) for item in lexical)


def test_empty_normalized_query_has_no_bm25_candidates(database) -> None:
    principal = _principal(database)
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        scope = _service(database).security.authorized_document_scope(connection, principal)
        assert RetrievalRepository().bm25_top10(connection, scope, ()) == ()


def test_missing_terms_never_fill_bm25_with_zero_scores(database) -> None:
    principal = _principal(database)
    _grant(database)
    _document(database, text="contenido sin coincidencia")
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        scope = _service(database).security.authorized_document_scope(connection, principal)
        assert RetrievalRepository().bm25_top10(connection, scope, ("lexema-inexistente",)) == ()


def test_zero_lexeme_fragment_participates_in_authorized_statistics(database) -> None:
    principal = _principal(database, "TI")
    normal = _document(database, text="contrato")
    empty = _document(database, text="— _ !!!")
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        for document in (normal, empty):
            SecurityRepository.set_document_exception(
                admin, account_id=principal.account_id, role_id=None,
                document_id=document["document"], decision="ALLOW", active=True,
            )
        receipt = admin.execute(
            "SELECT lexical_term_count FROM app.document_chunk_index_receipt WHERE fragment_id = %s",
            (empty["fragment"],),
        ).fetchone()
        assert receipt[0] == 0
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        scope = _service(database).security.authorized_document_scope(connection, principal)
        vector = RetrievalRepository().vector_top10(connection, scope, _Encoder().encode_query("contrato"))
        lexical = RetrievalRepository().bm25_top10(connection, scope, ("contrato",))
    assert {item.fragment_id for item in vector} == {normal["fragment"], empty["fragment"]}
    assert [item.fragment_id for item in lexical] == [normal["fragment"]]
    idf = math.log(1.0 + (2.0 - 1.0 + 0.5) / (1.0 + 0.5))
    expected = idf * 2.2 / (1.0 + 1.2 * (0.25 + 0.75 * 1.0 / 0.5))
    assert math.isclose(lexical[0].score, expected, rel_tol=1e-12)


def test_bm25_matches_independent_multi_term_tf_length_oracle(database) -> None:
    principal = _principal(database, "TI")
    documents = [
        _document(database, text="alfa alfa beta"),
        _document(database, text="alfa gamma"),
        _document(database, text="beta beta beta gamma"),
    ]
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        for document in documents:
            SecurityRepository.set_document_exception(
                admin, account_id=principal.account_id, role_id=None,
                document_id=document["document"], decision="ALLOW", active=True,
            )
    query = lexical_term_frequencies("alfa beta alfa")
    assert [(term.normalized_lexeme, term.term_frequency) for term in query] == [("alfa", 2), ("beta", 1)]
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        scope = _service(database).security.authorized_document_scope(connection, principal)
        actual = RetrievalRepository().bm25_top10(
            connection, scope, tuple(term.normalized_lexeme for term in query)
        )

    term_maps = ({"alfa": 2, "beta": 1}, {"alfa": 1}, {"beta": 3})
    lengths = (3, 2, 4)
    n, avgdl = 3.0, 3.0
    frequencies = {term: sum(term in mapping for mapping in term_maps) for term in ("alfa", "beta")}
    def oracle(index: int) -> float:
        total = 0.0
        for term in ("alfa", "beta"):
            tf = term_maps[index].get(term, 0)
            if not tf:
                continue
            df = frequencies[term]
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            total += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * lengths[index] / avgdl))
        return total
    expected = sorted(
        ((documents[index]["fragment"], oracle(index)) for index in range(3)),
        key=lambda pair: (-pair[1], str(pair[0])),
    )
    assert [item.fragment_id for item in actual] == [fragment for fragment, _ in expected]
    assert all(math.isclose(item.score, score, rel_tol=1e-12) for item, (_, score) in zip(actual, expected))


def test_representative_authorized_retrieval_latency_and_evidence(database) -> None:
    principal = _principal(database, "TI")
    allowed = [
        _document(database, text="alfa alfa beta", vector=(0.95, 0.05)),
        _document(database, text="alfa gamma", vector=(0.8, 0.2)),
        _document(database, text="beta beta beta gamma", vector=(0.6, 0.4)),
    ]
    restricted = _document(database, text="alfa beta secreto", vector=(1.0, 0.0))
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        for document in allowed:
            SecurityRepository.set_document_exception(
                admin, account_id=principal.account_id, role_id=None,
                document_id=document["document"], decision="ALLOW", active=True,
            )
    repository = RetrievalRepository()
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        scope = _service(database).security.authorized_document_scope(connection, principal)
        started = perf_counter()
        vector = repository.vector_top10(connection, scope, _Encoder().encode_query("alfa beta"))
        vector_seconds = perf_counter() - started
        started = perf_counter()
        lexical = repository.bm25_top10(connection, scope, ("alfa", "beta"))
        bm25_seconds = perf_counter() - started
        result = _service(database, repository=repository).retrieve(
            connection, RetrievalContext(principal, "alfa beta", uuid4(), uuid4())
        )
    allowed_ids = {item["fragment"] for item in allowed}
    assert len(vector) == len(lexical) == 3
    assert {item.fragment_id for item in (*vector, *lexical)} == allowed_ids
    assert restricted["fragment"] not in allowed_ids
    assert result.state is EvidenceState.EVIDENCIA_SUFICIENTE
    assert 0 < len(result.fragments) <= 5
    assert all(item.citation.complete and item.fragment_id in allowed_ids for item in result.fragments)
    assert any(item.vector_rank is not None and item.bm25_rank is not None for item in result.fragments)
    assert [item.rrf_score for item in result.fragments] == sorted(
        (item.rrf_score for item in result.fragments), reverse=True
    )
    no_access = _principal(database, "TI")
    assert _retrieve(database, no_access, "alfa beta").state is EvidenceState.SIN_EVIDENCIA
    print(f"representative_vector_s={vector_seconds:.6f} representative_bm25_s={bm25_seconds:.6f}")


def test_uncertified_and_invalidated_documents_are_excluded(database) -> None:
    principal = _principal(database)
    _grant(database)
    uncertified = _document(database, text="uncertified", certified=False)
    invalid = _document(database, text="invalidated")
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        connection.execute(
            "UPDATE app.document SET active_version_id = NULL, invalidated_at = CURRENT_TIMESTAMP "
            "WHERE id_documento = %s", (invalid["document"],),
        )
    result = _retrieve(database, principal, "uncertified invalidated")
    assert uncertified["fragment"] is None
    assert all(item.document_id not in {uncertified["document"], invalid["document"]} for item in result.fragments)


def test_partial_index_without_certificate_or_receipt_is_excluded(database) -> None:
    principal = _principal(database, "TI")
    partial = _document(database, text="índice parcial", certified=False)
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        admin.execute(
            "INSERT INTO app.document_chunk (id, document_version_id, ordinal, content, token_count, embedding) "
            "VALUES (%s, %s, 0, 'índice parcial', 2, %s::vector)",
            (uuid4(), partial["version"], "[1," + ",".join(["0"] * 1023) + "]"),
        )
        admin.execute(
            "UPDATE app.document_version SET processing_state = 'LISTA', completed_at = CURRENT_TIMESTAMP WHERE id = %s",
            (partial["version"],),
        )
        admin.execute("UPDATE app.document SET active_version_id = %s WHERE id_documento = %s", (partial["version"], partial["document"]))
        SecurityRepository.set_document_exception(
            admin, account_id=principal.account_id, role_id=None,
            document_id=partial["document"], decision="ALLOW", active=True,
        )
    assert _retrieve(database, principal, "índice parcial").state is EvidenceState.SIN_EVIDENCIA


def test_certified_receipt_and_certificate_cannot_be_corrupted(database) -> None:
    certified = _document(database, text="certificado íntegro")
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        with pytest.raises(psycopg.Error):
            admin.execute(
                "DELETE FROM app.document_chunk_index_receipt WHERE fragment_id = %s",
                (certified["fragment"],),
            )
        with pytest.raises(psycopg.Error):
            admin.execute(
                "UPDATE app.document_index_certificate SET expected_chunk_count = 2 WHERE document_version_id = %s",
                (certified["version"],),
            )
        assert admin.execute(
            "SELECT count(*) FROM app.document_chunk_index_receipt WHERE fragment_id = %s",
            (certified["fragment"],),
        ).fetchone()[0] == 1


def test_rejected_and_quarantined_sources_are_excluded(database) -> None:
    principal = _principal(database, "TI")
    rejected = _document(database, text="rejected source")
    quarantined = _document(database, text="quarantined source")
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        for document in (rejected, quarantined):
            SecurityRepository.set_document_exception(
                admin, account_id=principal.account_id, role_id=None,
                document_id=document["document"], decision="ALLOW", active=True,
            )
        admin.execute(
            "UPDATE app.ingest_file SET state = 'RECHAZADO', technical_result = 'REJECTED', "
            "safe_cause_code = 'TEST_REJECTED' WHERE id = %s",
            (rejected["file"],),
        )
        admin.execute(
            "INSERT INTO app.quarantine_item (id, ingest_file_id, cause_code, original_object_id, operation_id, correlation_id) "
            "VALUES (%s, %s, 'TEST_QUARANTINE', %s, %s, %s)",
            (uuid4(), quarantined["file"], quarantined["stored"], uuid4(), uuid4()),
        )
    result = _retrieve(database, principal, "source")
    assert result.state is EvidenceState.SIN_EVIDENCIA
    assert result.fragments == ()


def test_explicit_deny_overrides_family_and_allow(database) -> None:
    principal = _principal(database)
    _grant(database)
    document = _document(database, text="denegación explícita")
    with psycopg.connect(database["owner"], autocommit=True) as connection:
        SecurityRepository.set_document_exception(
            connection, account_id=principal.account_id, role_id=None,
            document_id=document["document"], decision="ALLOW", active=True,
        )
        SecurityRepository.set_document_exception(
            connection, account_id=None, role_id="JURIDICO",
            document_id=document["document"], decision="DENY", active=True,
        )
    result = _retrieve(database, principal, "denegación")
    assert all(item.document_id != document["document"] for item in result.fragments)


def test_document_specific_revocation_discards_ranked_result(database) -> None:
    principal = _principal(database, "TI")
    document = _document(database, text="revocación concurrente")
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        SecurityRepository.set_document_exception(
            admin, account_id=principal.account_id, role_id=None,
            document_id=document["document"], decision="ALLOW", active=True,
        )

    class RevokingRepository(RetrievalRepository):
        def bm25_top10(self, connection, scope, terms):
            assert document["fragment"] in {
                item.fragment_id for item in self.vector_top10(connection, scope, _Encoder().encode_query("revocación"))
            }
            with psycopg.connect(database["owner"], autocommit=True) as admin:
                SecurityRepository.set_document_exception(
                    admin, account_id=principal.account_id, role_id=None,
                    document_id=document["document"], decision="DENY", active=True,
                )
            return super().bm25_top10(connection, scope, terms)

    with pytest.raises(AuthorizationError, match="Acceso no autorizado"):
        _retrieve(database, principal, "revocación", repository=RevokingRepository())


def test_family_grant_revoked_during_final_corpus_check_discards_result(database) -> None:
    principal = _principal(database, "TI")
    document = _document(database, family="AUDITORIA_INTERNA", text="revocación familiar", vector=(1.0, 0.0))
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        SecurityRepository.set_scope_grant(admin, "TI", "AUDITORIA_INTERNA", True)

    class RevokingRepository(RetrievalRepository):
        def final_fragments_still_authorized(self, connection, scope, fragment_ids):
            assert document["fragment"] in fragment_ids
            assert super().final_fragments_still_authorized(connection, scope, fragment_ids)
            with psycopg.connect(database["owner"], autocommit=True) as admin:
                SecurityRepository.set_scope_grant(admin, "TI", "AUDITORIA_INTERNA", False)
            return True

    with pytest.raises(AuthorizationError, match="Acceso no autorizado"):
        _retrieve(database, principal, "revocación", repository=RevokingRepository())


def test_incomplete_citation_yields_state_two_without_dropping_candidate(database) -> None:
    principal = _principal(database, "TI")
    document = _document(database, text="cita sin fecha", document_date=None)
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        SecurityRepository.set_document_exception(
            admin, account_id=principal.account_id, role_id=None,
            document_id=document["document"], decision="ALLOW", active=True,
        )
    result = _retrieve(database, principal, "cita")
    assert result.state is EvidenceState.EVIDENCIA_INSUFICIENTE
    assert [item.fragment_id for item in result.fragments] == [document["fragment"]]
    assert result.fragments[0].citation.document_date is None
    assert result.fragments[0].citation.page_start is None
    assert result.fragments[0].citation.page_end is None


def test_default_deny_and_multiple_roles_use_current_security_scope(database) -> None:
    principal = _principal(database, "TI")
    document = _document(database, text="multirol autorizado", family="AUDITORIA_INTERNA")
    assert _retrieve(database, principal, "multirol").state is EvidenceState.SIN_EVIDENCIA
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        admin.execute(
            "INSERT INTO app.user_role (user_id, role_id) VALUES (%s, 'ANALISTA')",
            (principal.account_id,),
        )
        SecurityRepository.set_scope_grant(admin, "ANALISTA", "AUDITORIA_INTERNA", True)
    result = _retrieve(database, principal, "multirol")
    assert document["fragment"] in {item.fragment_id for item in result.fragments}
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        SecurityRepository.set_document_exception(
            admin, account_id=principal.account_id, role_id=None,
            document_id=document["document"], decision="DENY", active=True,
        )
    assert document["fragment"] not in {
        item.fragment_id for item in _retrieve(database, principal, "multirol").fragments
    }


def test_superseded_certified_version_is_excluded(database) -> None:
    principal = _principal(database, "TI")
    original = _document(database, text="versión anterior")
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        SecurityRepository.set_document_exception(
            admin, account_id=principal.account_id, role_id=None,
            document_id=original["document"], decision="ALLOW", active=True,
        )
    assert original["fragment"] in {
        item.fragment_id for item in _retrieve(database, principal, "versión").fragments
    }
    replacement_id = uuid4()
    replacement_text = "versión nueva"
    replacement_hash = hashlib.sha256(replacement_text.encode("utf-8")).digest()
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        admin.execute(
            "INSERT INTO app.document_version "
            "(id, id_documento, version_number, stored_object_id, processing_state, consolidated_text, "
            "content_sha256, operation_id, correlation_id) "
            "VALUES (%s, %s, 2, %s, 'PROCESANDO', %s, %s, %s, %s)",
            (replacement_id, original["document"], original["stored"], replacement_text, replacement_hash, uuid4(), uuid4()),
        )
    replacement = IndexingService(IndexRepository(database["owner"]), _Encoder()).index_version(
        document_version_id=replacement_id, consolidated_text=replacement_text, tokenizer=FastTokenizer()
    )
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        admin.execute(
            "UPDATE app.document SET active_version_id = %s WHERE id_documento = %s",
            (replacement_id, original["document"]),
        )
    result = _retrieve(database, principal, "versión")
    assert replacement.fragment_ids[0] in {item.fragment_id for item in result.fragments}
    assert original["fragment"] not in {item.fragment_id for item in result.fragments}


def test_rf048_identifier_is_neutral_to_postgres_retrieval(database) -> None:
    principal = _principal(database, "TI")
    document = _document(database, text="contexto neutral")
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        SecurityRepository.set_document_exception(
            admin, account_id=principal.account_id, role_id=None,
            document_id=document["document"], decision="ALLOW", active=True,
        )
    without_reference = _retrieve(database, principal, "contexto")
    reference_id = uuid4()
    with_reference = _retrieve(database, principal, "contexto", context_reference_id=reference_id)
    assert with_reference.context_reference_id == reference_id
    assert with_reference.state == without_reference.state
    assert [item.fragment_id for item in with_reference.fragments] == [
        item.fragment_id for item in without_reference.fragments
    ]


def test_capacity_50000_certified_authorized_fragments(database) -> None:
    """Mide el corpus sintético de 50k; el entorno de referencia se evalúa aparte."""
    principal = _principal(database, "TI")
    capacity = _document(database, text="capacidad sintética", certified=False)
    count = 50_000
    vector_literal = "[1," + ",".join(["0"] * 1023) + "]"
    text = "\n".join("capacidad" for _ in range(count))
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    identifiers = [uuid4() for _ in range(count)]
    prepare_start = perf_counter()
    with psycopg.connect(database["owner"], autocommit=True) as admin:
        with admin.transaction():
            admin.execute(
                "UPDATE app.document_version SET consolidated_text = %s, content_sha256 = %s, "
                "processing_state = 'LISTA', completed_at = CURRENT_TIMESTAMP WHERE id = %s",
                (text, digest, capacity["version"]),
            )
            with admin.cursor() as cursor:
                with cursor.copy(
                    "COPY app.document_chunk (id, document_version_id, ordinal, content, token_count, embedding) FROM STDIN"
                ) as copy:
                    for index, identity in enumerate(identifiers):
                        copy.write_row((identity, capacity["version"], index, "capacidad", 1, vector_literal))
                with cursor.copy(
                    "COPY app.chunk_term (fragment_id, normalized_lexeme, term_frequency) FROM STDIN"
                ) as copy:
                    for identity in identifiers:
                        copy.write_row((identity, "capacidad", 1))
                with cursor.copy(
                    "COPY app.document_chunk_index_receipt "
                    "(fragment_id, index_contract_version, lexical_term_count, processed_at) FROM STDIN"
                ) as copy:
                    for identity in identifiers:
                        copy.write_row((
                            identity, "bge-m3:d1024:c512:o50:lex-nfkc-casefold-alnum-v1", 1, datetime.now(UTC)
                        ))
            admin.execute(
                "INSERT INTO app.document_index_certificate "
                "(document_version_id, index_contract_version, embedding_model, embedding_dimensions, "
                "chunk_size_tokens, chunk_overlap_tokens, expected_chunk_count, embedded_chunk_count, "
                "lexical_processed_chunk_count, zero_lexeme_chunk_count, source_text_sha256) "
                "VALUES (%s, 'bge-m3:d1024:c512:o50:lex-nfkc-casefold-alnum-v1', 'BAAI/bge-m3', "
                "1024, 512, 50, %s, %s, %s, 0, %s)",
                (capacity["version"], count, count, count, digest),
            )
            admin.execute(
                "UPDATE app.document SET active_version_id = %s WHERE id_documento = %s",
                (capacity["version"], capacity["document"]),
            )
            SecurityRepository.set_document_exception(
                admin, account_id=principal.account_id, role_id=None,
                document_id=capacity["document"], decision="ALLOW", active=True,
            )
    prepare_seconds = perf_counter() - prepare_start
    with psycopg.connect(database["runtime"], autocommit=True, row_factory=dict_row) as connection:
        scope = _service(database).security.authorized_document_scope(connection, principal)
        repository = RetrievalRepository()
        corpus_sql, corpus_parameters = repository._corpus(scope)
        with connection.cursor(row_factory=tuple_row) as cursor:
            cursor.execute(
                "EXPLAIN " + corpus_sql +
                "SELECT fragment_id FROM authorized_corpus "
                "ORDER BY embedding <=> %(query_vector)s::vector, fragment_id LIMIT 10",
                {**corpus_parameters, "query_vector": vector_literal},
            )
            explain_lines = tuple(str(row[0]) for row in cursor.fetchall())
            bm25_sql, bm25_parameters = repository._bm25_statement(scope)
            cursor.execute(
                "EXPLAIN " + bm25_sql,
                {**bm25_parameters, "query_terms": ["capacidad"]},
            )
            bm25_explain_lines = tuple(str(row[0]) for row in cursor.fetchall())
        started = perf_counter()
        vector = repository.vector_top10(connection, scope, _Encoder().encode_query("capacidad"))
        vector_seconds = perf_counter() - started
        started = perf_counter()
        lexical = repository.bm25_top10(connection, scope, ("capacidad",))
        bm25_seconds = perf_counter() - started
        current = connection.execute(
            "SELECT count(*) FROM app.document_chunk WHERE document_version_id = %s", (capacity["version"],)
        ).fetchone()["count"]
    print(
        f"CAPACITY_50000 count={count} prepare_s={prepare_seconds:.3f} "
        f"vector_s={vector_seconds:.3f} bm25_s={bm25_seconds:.3f} "
        f"vector_results={len(vector)} bm25_results={len(lexical)} "
        f"vector_explain_nodes={len(explain_lines)} bm25_explain_nodes={len(bm25_explain_lines)}"
    )
    assert any("CTE eligible_versions" in line for line in explain_lines)
    assert any("CTE terms_with_lengths" in line for line in bm25_explain_lines)
    assert current == count
    assert len(vector) == len(lexical) == 10
    assert all(item.citation.document_id == capacity["document"] for item in vector + lexical)


@pytest.mark.requires_model
@pytest.mark.contract
def test_real_bge_m3_query_embedding_has_1024_finite_components() -> None:
    from app.embeddings import get_embedding_service

    vector = get_embedding_service().encode_query("consulta documental de prueba")
    assert len(vector) == 1024
    assert all(math.isfinite(value) for value in vector)
