"""Comprobaciones estáticas y de rechazo temprano del provisioning explícito."""
from pathlib import Path

import pytest

from scripts.provision_audit_reader import ProvisioningError, provision


@pytest.mark.parametrize("login,password", [("", "synthetic"), ("riesgo_legal_audit_reader", "synthetic"),
                                          ("app", "synthetic"), ("riesgo_legal_app", "synthetic"), ("reader", "")])
def test_unsafe_identity_rejected_before_sql(login, password):
    with pytest.raises(ProvisioningError):
        provision(None, login=login, password=password)


def test_provisioning_does_not_grant_domain_dml_or_run_migrations():
    source = (Path(__file__).parents[2] / "scripts" / "provision_audit_reader.py").read_text(encoding="utf-8")
    assert "GRANT {} TO {} WITH INHERIT TRUE" in source
    assert "GRANT SELECT" not in source and "GRANT INSERT" not in source
    assert "alembic" not in source
    assert "print(password" not in source and "print(payload" not in source
    assert "input=json.dumps(payload), capture_output=True" in source
    assert "NOT LIKE 'pg_%%'" in source
    assert "NOT LIKE 'pg_%'" not in source


def test_reader_queries_are_schema_qualified():
    source = (Path(__file__).parents[2] / "app" / "audit" / "repository.py").read_text(encoding="utf-8")
    assert "FROM audit.event e" in source and "FROM audit.event_resource" in source
    assert "SET TRANSACTION READ ONLY" in source
    assert "SET LOCAL search_path = pg_catalog" in source


def test_all_provisioning_entries_protect_password_statements():
    import inspect
    source = inspect.getsource(provision)
    first_ddl = source.index("CREATE ROLE")
    for setting in ("SET LOCAL log_statement", "SET LOCAL log_min_duration_statement",
                    "SET LOCAL log_min_error_statement"):
        assert source.index(setting) < first_ddl


def test_cli_without_protected_file_fails_before_connection(monkeypatch):
    from scripts.provision_audit_reader import main
    monkeypatch.setattr("sys.argv", ["provision_audit_reader.py"])
    with pytest.raises(ProvisioningError, match="procedimiento protegido único"):
        main()
