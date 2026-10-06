"""Aprovisionamiento explícito de un lector; los permisos de tablas ya deben existir."""
from __future__ import annotations

import os
import argparse
import json
from pathlib import Path
import secrets
import subprocess


READER_GROUP = "riesgo_legal_audit_reader"
EXPECTED_LOGIN = (True, True, False, False, False, False)


class ProvisioningError(RuntimeError):
    """La identidad lectora no satisface la separación de privilegios."""

    def __init__(self, message: str, *, stage: str = "PRECONDITION", sqlstate: str | None = None, code: str = "INVALID_READER_CONTRACT"):
        super().__init__(message)
        self.stage = stage
        self.sqlstate = sqlstate if sqlstate and len(sqlstate) == 5 and sqlstate.isalnum() else None
        self.code = code


def validate_reader_privileges(connection, login: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = %s", (login,))
        attributes = cursor.fetchone()
        if attributes is None or attributes[2:] != (False, False, False, False):
            raise ProvisioningError("Atributos del lector no válidos", code="READER_ATTRIBUTES")
        cursor.execute("SELECT pg_catalog.has_database_privilege(%s, current_database(), 'CREATE')", (login,))
        if cursor.fetchone()[0]:
            raise ProvisioningError("El lector puede crear objetos persistentes", code="DATABASE_CREATE")
        cursor.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n
                           WHERE n.nspname NOT LIKE 'pg_%%' AND n.nspname <> 'information_schema'
                           AND (pg_catalog.has_schema_privilege(%s,n.oid,'CREATE')
                                OR pg_catalog.pg_has_role(%s,n.nspowner,'USAGE')))""", (login, login))
        if cursor.fetchone()[0]:
            raise ProvisioningError("El lector controla un esquema persistente", code="SCHEMA_CREATE_OR_OWNERSHIP")
        cursor.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_class c
                           JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                           WHERE n.nspname NOT LIKE 'pg_%%' AND n.nspname <> 'information_schema'
                           AND pg_catalog.pg_has_role(%s,c.relowner,'USAGE'))""", (login,))
        if cursor.fetchone()[0]:
            raise ProvisioningError("El lector controla objetos persistentes", code="OBJECT_OWNERSHIP")
        for relation in ("audit.event", "audit.event_resource"):
            cursor.execute("SELECT pg_catalog.has_table_privilege(%s,%s,'SELECT')", (login, relation))
            if not cursor.fetchone()[0]:
                raise ProvisioningError("El grupo lector carece de permisos de lectura", code="AUDIT_SELECT_MISSING")
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                cursor.execute("SELECT pg_catalog.has_table_privilege(%s,%s,%s)", (login, relation, privilege))
                if cursor.fetchone()[0]:
                    raise ProvisioningError("El lector puede modificar la bitácora", code="AUDIT_MUTATION")
        cursor.execute("""SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_class c
                           JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                           WHERE n.nspname='app' AND c.relkind IN ('r','v','m','p')
                           AND (pg_catalog.has_table_privilege(%s,c.oid,'SELECT')
                            OR pg_catalog.has_table_privilege(%s,c.oid,'INSERT')
                            OR pg_catalog.has_table_privilege(%s,c.oid,'UPDATE')
                            OR pg_catalog.has_table_privilege(%s,c.oid,'DELETE')
                            OR pg_catalog.has_table_privilege(%s,c.oid,'TRUNCATE')))""", (login,) * 5)
        if cursor.fetchone()[0]:
            raise ProvisioningError("El lector tiene acceso a tablas fuera de auditoría", code="DOMAIN_TABLE_ACCESS")


def provision(connection, *, login: str, password: str) -> None:
    from psycopg import sql
    if not login or login in (READER_GROUP, "app", "riesgo_legal_app") or not password:
        raise ProvisioningError("Se requiere una identidad lectora independiente y secreto protegido")
    with connection.transaction():
        with connection.cursor() as cursor:
            # Toda entrada del operador protege el secreto antes de cualquier DDL.
            cursor.execute("SET LOCAL log_statement = 'none'")
            cursor.execute("SET LOCAL log_min_duration_statement = -1")
            cursor.execute("SET LOCAL log_min_error_statement = 'panic'")
            cursor.execute("SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s", (READER_GROUP,))
            group = cursor.fetchone()
            if group is None or group[0] or group[2:] != (False, False, False, False):
                raise ProvisioningError("El grupo lector existente no es válido")
            validate_reader_privileges(connection, READER_GROUP)
            cursor.execute("SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s", (login,))
            existing = cursor.fetchone()
            if existing is not None and existing != EXPECTED_LOGIN:
                raise ProvisioningError("La identidad existente tiene atributos no permitidos")
            cursor.execute("""SELECT r.rolname, m.admin_option FROM pg_catalog.pg_auth_members m
                              JOIN pg_catalog.pg_roles r ON r.oid=m.roleid
                              JOIN pg_catalog.pg_roles u ON u.oid=m.member WHERE u.rolname=%s""", (login,))
            if any(name != READER_GROUP or admin for name, admin in cursor.fetchall()):
                raise ProvisioningError("La identidad existente tiene membresías no permitidas")
            if existing is None:
                cursor.execute(sql.SQL("CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}").format(sql.Identifier(login), sql.Literal(password)))
            else:
                cursor.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(login), sql.Literal(password)))
            cursor.execute(sql.SQL("GRANT {} TO {} WITH INHERIT TRUE").format(sql.Identifier(READER_GROUP), sql.Identifier(login)))
            validate_reader_privileges(connection, login)
            cursor.execute("SELECT pg_catalog.pg_has_role(%s,%s,'MEMBER'),pg_catalog.pg_has_role(%s,%s,'USAGE')", (login, READER_GROUP, login, READER_GROUP))
            if cursor.fetchone() != (True, True):
                raise ProvisioningError("La membresía lectora no pudo verificarse")


def provision_protected_payload(payload: dict) -> dict:
    """Solo devuelve resultados técnicos; el secreto permanece en el proceso."""
    import psycopg
    stage = "OWNER_CONNECTION"
    try:
        parameters = dict(host=payload["host"], port=int(payload["port"]), dbname=payload["database"],
                          user=payload["owner"], password=payload["owner_password"], connect_timeout=5)
        with psycopg.connect(**parameters) as connection:
            stage = "OWNER_IDENTITY"
            if connection.execute("SELECT current_user").fetchone()[0] != payload["owner"]:
                raise ProvisioningError("Identidad de aprovisionamiento no válida")
            stage = "GROUP_ATTRIBUTES"
            group = connection.execute("SELECT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname=%s", (READER_GROUP,)).fetchone()
            if group != (False,):
                raise ProvisioningError("Grupo lector no válido")
            stage = "GROUP_PRIVILEGES"
            validate_reader_privileges(connection, READER_GROUP)
            stage = "LOGIN_EXISTENCE"
            exists = connection.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=%s", (payload["login"],)).fetchone() is not None
            if exists and not payload["configured"]:
                raise ProvisioningError("Identidad existente sin configuración protegida; no se rotará")
            if not exists:
                stage = "SESSION_LOG_PROTECTION"
                # Nunca registrar la sentencia que establece material secreto.
                connection.execute("SET LOCAL log_statement = 'none'")
                connection.execute("SET LOCAL log_min_duration_statement = -1")
                connection.execute("SET LOCAL log_min_error_statement = 'panic'")
                stage = "PROVISION_ROLE"
                provision(connection, login=payload["login"], password=payload["password"])
            else:
                stage = "EXISTING_LOGIN_VALIDATION"
                attributes = connection.execute("SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s", (payload["login"],)).fetchone()
                memberships = connection.execute("""SELECT r.rolname,m.admin_option FROM pg_catalog.pg_auth_members m
                    JOIN pg_catalog.pg_roles r ON r.oid=m.roleid JOIN pg_catalog.pg_roles u ON u.oid=m.member
                    WHERE u.rolname=%s""", (payload["login"],)).fetchall()
                if attributes != EXPECTED_LOGIN or memberships != [(READER_GROUP, False)]:
                    raise ProvisioningError("Identidad existente no conforme")
                validate_reader_privileges(connection, payload["login"])
        parameters.update(user=payload["login"], password=payload["password"])
        stage = "READER_TCP_AUTHENTICATION"
        with psycopg.connect(**parameters) as reader:
            stage = "READER_IDENTITY"
            if reader.execute("SELECT session_user,current_user").fetchone() != (payload["login"], payload["login"]):
                raise ProvisioningError("Autenticación lectora no conforme")
            stage = "READER_SELECT"
            reader.execute("SELECT id FROM audit.event LIMIT 1").fetchall()
            stage = "READER_DENIED_OPERATIONS"
            for statement in ("INSERT INTO audit.event DEFAULT VALUES", "UPDATE audit.event SET result='FAILURE' WHERE false",
                              "DELETE FROM audit.event WHERE false", "CREATE TABLE app.audit_forbidden(id integer)"):
                try:
                    with reader.transaction():
                        reader.execute(statement)
                except psycopg.errors.InsufficientPrivilege:
                    continue
                raise ProvisioningError("La identidad lectora puede efectuar una operación prohibida")
        return {"provisioning": "PASS", "tcp_authentication": "PASS", "reader_privileges": "PASS", "created": not exists}
    except ProvisioningError as exc:
        raise ProvisioningError(str(exc), stage=stage, code=exc.code) from None
    except psycopg.Error as exc:
        raise ProvisioningError("Aprovisionamiento lector no confirmado", stage=stage, sqlstate=exc.sqlstate) from None
    except (KeyError, TypeError, ValueError):
        raise ProvisioningError("Aprovisionamiento lector no confirmado", stage=stage) from None


def provision_env_file(path: Path, *, docker_container: str, backend_path: str, connect_host: str) -> None:
    """Actualiza únicamente dos claves en el archivo ignorado conservando su identidad y ACL."""
    path = path.resolve()
    if path.name != ".env" or not path.is_file():
        raise ProvisioningError("Se requiere un archivo local protegido existente")
    ignored = subprocess.run(["git", "-C", str(path.parent), "check-ignore", "--quiet", ".env"], capture_output=True)
    if ignored.returncode != 0:
        raise ProvisioningError("La configuración protegida debe estar excluida de Git")
    content = path.read_text(encoding="utf-8")
    configuration = {}
    for line in content.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.lstrip().startswith("#"):
            configuration[key.strip()] = value.strip().strip('\"').strip("'")
    configured = bool(configuration.get("AUDIT_POSTGRES_PASSWORD"))
    login = configuration.get("AUDIT_POSTGRES_USER") or "riesgo_legal_audit_app"
    if login != "riesgo_legal_audit_app":
        raise ProvisioningError("Identidad lectora distinta del contrato aprobado")
    password = configuration.get("AUDIT_POSTGRES_PASSWORD") or secrets.token_urlsafe(48)
    payload = dict(host=connect_host, port=5432, database=configuration.get("POSTGRES_DB", "riesgo_legal"),
                   owner=configuration.get("POSTGRES_USER", ""), owner_password=configuration.get("POSTGRES_PASSWORD", ""),
                   login=login, password=password, configured=configured)
    if not payload["owner"] or not payload["owner_password"] or payload["owner"] == login:
        raise ProvisioningError("La configuración de aprovisionamiento no es válida")
    # El transporte recibe secretos por stdin, nunca por argumentos ni salida visible.
    program = """import json,sys
sys.path.insert(0,sys.argv[1])
from scripts.provision_audit_reader import provision_protected_payload,ProvisioningError
try:
 result=provision_protected_payload(json.load(sys.stdin))
 print(json.dumps(result))
except ProvisioningError as exc:
 print(json.dumps({"provisioning":"FAIL","stage":exc.stage,"sqlstate":exc.sqlstate,"code":exc.code}))
 sys.exit(1)
except Exception:
 print('{"provisioning":"FAIL","stage":"SAFE_UNEXPECTED_FAILURE"}')
 sys.exit(1)
"""
    execution = subprocess.run(["docker", "exec", "-i", docker_container, "python", "-c", program, backend_path],
                               input=json.dumps(payload), capture_output=True, text=True)
    if execution.returncode != 0:
        try:
            failure = json.loads(execution.stdout)
            stage = failure.get("stage")
            if stage not in {"OWNER_CONNECTION", "OWNER_IDENTITY", "GROUP_ATTRIBUTES", "GROUP_PRIVILEGES", "LOGIN_EXISTENCE",
                             "SESSION_LOG_PROTECTION", "PROVISION_ROLE", "EXISTING_LOGIN_VALIDATION", "READER_TCP_AUTHENTICATION",
                             "READER_IDENTITY", "READER_SELECT", "READER_DENIED_OPERATIONS", "SAFE_UNEXPECTED_FAILURE"}:
                stage = "TRANSPORT_FAILURE"
            code = failure.get("code")
            if code not in {"READER_ATTRIBUTES", "DATABASE_CREATE", "SCHEMA_CREATE_OR_OWNERSHIP", "OBJECT_OWNERSHIP", "AUDIT_SELECT_MISSING", "AUDIT_MUTATION", "DOMAIN_TABLE_ACCESS"}:
                code = "INVALID_READER_CONTRACT"
            raise ProvisioningError("Aprovisionamiento lector no confirmado", stage=stage, sqlstate=failure.get("sqlstate"), code=code)
        except (ValueError, AttributeError, TypeError):
            raise ProvisioningError("Aprovisionamiento lector no confirmado", stage="TRANSPORT_FAILURE") from None
    try:
        result = json.loads(execution.stdout)
        if result.get("provisioning") != "PASS":
            raise ValueError
    except (ValueError, AttributeError):
        raise ProvisioningError("Respuesta de aprovisionamiento no válida") from None
    lines = [line for line in content.splitlines() if line.partition("=")[0].strip() not in {"AUDIT_POSTGRES_USER", "AUDIT_POSTGRES_PASSWORD"}]
    updated = "\n".join([*lines, f"AUDIT_POSTGRES_USER={login}", f"AUDIT_POSTGRES_PASSWORD={password}", ""])
    # Escribir en el mismo archivo preserva su ACL; no crear copias con credenciales.
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description="Aprovisionar una identidad lectora independiente")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--docker-container", default="riesgo-legal-backend")
    parser.add_argument("--backend-path", default="/tmp/backend_final_build")
    parser.add_argument("--connect-host", default="postgres")
    arguments = parser.parse_args()
    if arguments.env_file:
        provision_env_file(arguments.env_file, docker_container=arguments.docker_container,
                           backend_path=arguments.backend_path, connect_host=arguments.connect_host)
        print("AUDIT_READER_PROVISIONING=PASS; TCP_AUTHENTICATION=PASS; READER_PRIVILEGES=PASS")
        return
    raise ProvisioningError("Se requiere --env-file y el procedimiento protegido único")


if __name__ == "__main__":
    try:
        main()
    except ProvisioningError as exc:
        raise SystemExit(f"AUDIT_READER_PROVISIONING=FAIL; STAGE={exc.stage}; CODE={exc.code}; SQLSTATE={exc.sqlstate or 'NONE'}") from None
    except (OSError, subprocess.SubprocessError):
        raise SystemExit("No se confirmó la operación protegida") from None
