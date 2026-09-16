#!/usr/bin/env python3
"""Batería de smoke tests del entorno de desarrollo.

Comando único de verificación. Solo usa la librería estándar, de modo que pueda
ejecutarse en cualquier equipo con Python 3.10+ sin instalar dependencias.

Uso:
    python scripts/smoke.py                    # estático + runtime (requiere stack arriba)
    python scripts/smoke.py --static-only      # solo comprobaciones sin Docker
    python scripts/smoke.py --profile airflow  # stack completo, incluida la orquestación

Cada comprobación devuelve PASS / FAIL / SKIP con su evidencia. El script no oculta
fallos: si una comprobación no puede ejecutarse se marca SKIP y se registra como
evidencia pendiente, nunca como PASS.

Clasificación de las comprobaciones:
    CONTRACT    -> verifica una capacidad o propiedad que el sistema debe cumplir
                   (compatibilidad vectorial, dimensionalidad del modelo)
    ROBUSTNESS  -> control defensivo o de coherencia de la configuración
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_BRANCH = "main"
EXPECTED_COMMITTER_NAME = "Eduardo"
EXPECTED_COMMITTER_EMAIL = "cesaradame624@gmail.com"

# Topología esperada: exactamente cuatro servicios lógicos.
EXPECTED_LOGICAL_SERVICES = {"frontend", "backend", "postgres", "airflow"}

# Servicios que el sistema no necesita y que no deben declararse.
UNEXPECTED_SERVICES = {
    "redis",
    "rabbitmq",
    "elasticsearch",
    "opensearch",
    "milvus",
    "qdrant",
    "weaviate",
    "chroma",
    "embeddings",
    "embedding-service",
    "bge-m3",
}

# Configuración de referencia del equipo (16 GB de RAM, >=50 GB de almacenamiento).
REFERENCE_RAM_GB = 16.0
REFERENCE_DISK_GB = 50.0
EMBEDDING_DIMENSIONS = 1024

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class Check:
    """Resultado de una comprobación."""

    id: str
    title: str
    classification: str
    status: str
    evidence: str = ""

    def render(self) -> str:
        mark = {"PASS": "[OK]  ", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[self.status]
        return f"{mark} {self.id:<10} {self.title}\n               {self.evidence}"


@dataclass
class Report:
    """Informe agregado."""

    checks: list[Check] = field(default_factory=list)

    def add(self, check_id: str, title: str, classification: str, status: str, evidence: str) -> str:
        self.checks.append(Check(check_id, title, classification, status, evidence))
        return status

    @property
    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, SKIP: 0}
        for check in self.checks:
            out[check.status] += 1
        return out

    def render(self) -> str:
        lines = ["", "=" * 78, "SMOKE TEST — entorno de desarrollo", "=" * 78, ""]
        for check in self.checks:
            lines.append(check.render())
        counts = self.counts
        lines += [
            "",
            "-" * 78,
            f"PASS={counts[PASS]}  FAIL={counts[FAIL]}  SKIP={counts[SKIP]}",
        ]
        if counts[FAIL]:
            verdict = "ENTORNO NO VÁLIDO (hay fallos)"
        elif counts[SKIP]:
            verdict = "ENTORNO NO VERIFICADO POR COMPLETO (hay evidencia pendiente)"
        else:
            verdict = "ENTORNO VERIFICADO"
        lines.append(f"VEREDICTO: {verdict}")
        lines.append("-" * 78)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> tuple[int, str]:
    """Ejecuta un comando y devuelve (returncode, salida combinada)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd or PRODUCT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except FileNotFoundError as exc:
        return 127, f"comando no disponible: {exc.filename}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout tras {timeout}s"


def http_get(url: str, timeout: int = 10) -> tuple[int | None, str]:
    """GET simple. Devuelve (status, body) o (None, error) sin levantar excepción."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc.__class__.__name__}: {exc}"


def http_post(url: str, timeout: int = 600) -> tuple[int | None, str]:
    """POST sin cuerpo."""
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc.__class__.__name__}: {exc}"


def load_env() -> dict[str, str]:
    """Lee producto/.env sin exponer sus valores."""
    env: dict[str, str] = {}
    path = PRODUCT_ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def compose_services(profile: str | None) -> tuple[int, str]:
    cmd = ["docker", "compose"]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["config", "--services"]
    return run(cmd)


def logical_service(name: str) -> str:
    """Colapsa las variantes de un servicio materializado en varios contenedores."""
    return re.sub(r"-(init|scheduler|webserver|worker|triggerer)$", "", name)


# ---------------------------------------------------------------------------
# Grupo REPO — repositorio y secretos
# ---------------------------------------------------------------------------
def check_repository(report: Report) -> None:
    _, status = run(["git", "status", "--porcelain"])
    tracked_env = any(
        line.strip().split(maxsplit=1)[-1] == ".env"
        for line in status.splitlines()
        if line.strip()
    )
    report.add(
        "REPO-01", ".env no aparece como archivo rastreado", "ROBUSTNESS",
        FAIL if tracked_env else PASS,
        "git status --porcelain no lista .env"
        if not tracked_env else "FUGA: .env aparece en git status",
    )

    code, out = run(["git", "check-ignore", "-v", ".env"])
    report.add(
        "REPO-02", ".env está excluido por .gitignore", "ROBUSTNESS",
        PASS if code == 0 and out else FAIL,
        out or f"check-ignore exit={code} sin coincidencia",
    )

    code, ls_files = run(["git", "ls-files"])
    staged = [f for f in ls_files.splitlines() if f.strip()]
    secret_like = [
        f for f in staged
        if re.search(r"(^|/)\.env$|secret|credential|\.pem$|\.key$", f, re.I)
    ]

    # Análisis LÍNEA A LÍNEA de .env.example. Un regex multilínea produce falsos
    # positivos porque `\s*=` salta al siguiente nombre de variable.
    example_path = PRODUCT_ROOT / ".env.example"
    offenders: list[str] = []
    if example_path.exists():
        secret_keys = re.compile(r"(PASSWORD|SECRET|KEY|TOKEN)$", re.I)
        for lineno, raw in enumerate(example_path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not secret_keys.search(key):
                continue
            # Un placeholder aceptable está vacío o marcado como CHANGE_ME.
            if value and not value.startswith("CHANGE_ME"):
                offenders.append(f"{key} (línea {lineno})")

    ok = not secret_like and not offenders
    report.add(
        "REPO-03", "sin secretos en archivos rastreables", "ROBUSTNESS",
        PASS if ok else FAIL,
        "todas las variables sensibles de .env.example están vacías o marcadas CHANGE_ME"
        if ok else f"archivos={secret_like} valores_reales={offenders}",
    )

    _, branch = run(["git", "branch", "--show-current"])
    report.add(
        "REPO-04", f"rama principal == {EXPECTED_BRANCH}", "ROBUSTNESS",
        PASS if branch.strip() == EXPECTED_BRANCH else FAIL,
        f"rama actual: {branch.strip() or '(sin commits)'}",
    )

    _, name = run(["git", "config", "user.name"])
    _, email = run(["git", "config", "user.email"])
    identity_ok = (
        name.strip() == EXPECTED_COMMITTER_NAME
        and email.strip() == EXPECTED_COMMITTER_EMAIL
    )
    report.add(
        "REPO-05", "identidad de commit configurada", "ROBUSTNESS",
        PASS if identity_ok else FAIL,
        f"user.name={name.strip()!r} user.email={email.strip()!r}",
    )

    code, log = run(["git", "log", "--oneline", "-n", "1"])
    has_commits = code == 0 and bool(log.strip())
    report.add(
        "REPO-06", "el repositorio tiene al menos un commit", "ROBUSTNESS",
        PASS if has_commits else SKIP,
        log.strip().splitlines()[0] if has_commits else "el repositorio todavía no tiene commits",
    )


# ---------------------------------------------------------------------------
# Grupo TOPO — topología declarada
# ---------------------------------------------------------------------------
def check_topology(report: Report, profile: str | None) -> None:
    for label, prof in (("perfil por defecto", None), (f"perfil {profile}", profile)):
        code, out = run(
            ["docker", "compose"] + (["--profile", prof] if prof else []) + ["config", "-q"]
        )
        report.add(
            "TOPO-01", f"docker compose config válido ({label})", "ROBUSTNESS",
            PASS if code == 0 else FAIL,
            "exit 0" if code == 0 else out[:400],
        )

    # Se enumera la TOPOLOGÍA DECLARADA (todos los perfiles), no solo los
    # contenedores activos: con el perfil inactivo los servicios siguen declarados.
    code, out = compose_services("airflow")
    declared = {s.strip() for s in out.splitlines() if s.strip()}
    declared_logical = {logical_service(s) for s in declared}
    unexpected = {s for s in declared_logical if s.lower() in UNEXPECTED_SERVICES}

    code_active, out_active = compose_services(profile)
    active = {logical_service(s) for s in out_active.splitlines() if s.strip()}

    ok = (
        code == 0
        and declared_logical == EXPECTED_LOGICAL_SERVICES
        and not unexpected
    )
    report.add(
        "TOPO-02", "exactamente 4 servicios lógicos, sin infraestructura no prevista",
        "ROBUSTNESS",
        PASS if ok else FAIL,
        f"declarados={sorted(declared)} logicos={sorted(declared_logical)} "
        f"activos={sorted(active)} no_previstos={sorted(unexpected) or 'ninguno'}",
    )


# ---------------------------------------------------------------------------
# Grupo SVC — salud de los servicios
# ---------------------------------------------------------------------------
def check_runtime(report: Report, env: dict[str, str], profile: str | None) -> None:
    backend = f"http://localhost:{env.get('BACKEND_PORT', '8000')}"
    frontend = f"http://localhost:{env.get('FRONTEND_PORT', '3000')}"
    airflow = f"http://localhost:{env.get('AIRFLOW_PORT', '8080')}"

    status, body = http_get(f"{backend}/health", timeout=15)
    ok = status == 200 and '"status":"ok"' in body.replace(" ", "")
    report.add(
        "SVC-01", "backend responde en /health (200)", "ROBUSTNESS",
        PASS if ok else (SKIP if status is None else FAIL),
        f"HTTP {status}" + ("" if status is not None else f" — {body[:160]}"),
    )

    status, body = http_get(frontend, timeout=15)
    report.add(
        "SVC-02", "frontend sirve HTTP 200", "ROBUSTNESS",
        PASS if status == 200 else (SKIP if status is None else FAIL),
        f"HTTP {status}" if status is not None else body[:160],
    )

    # Disponibilidad de pgvector y compatibilidad de la dimensionalidad.
    #
    # La comprobación se ejecuta dentro de una transacción que termina en ROLLBACK,
    # de modo que no queda rastro: habilitar la extensión de forma permanente es un
    # acto de aprovisionamiento que corresponde a las migraciones de la aplicación.
    vector_literal = "[" + ",".join(["0"] * EMBEDDING_DIMENSIONS) + "]"
    sql = (
        "BEGIN; "
        "CREATE EXTENSION IF NOT EXISTS vector; "
        f"SELECT 'dims=' || vector_dims('{vector_literal}'::vector); "
        "SELECT 'ext_version=' || extversion FROM pg_extension WHERE extname='vector'; "
        "ROLLBACK;"
    )
    psql = ["docker", "compose", "exec", "-T", "postgres", "psql", "-U",
            env.get("POSTGRES_USER", "app"), "-d", env.get("POSTGRES_DB", "riesgo_legal")]
    code, out = run(psql + ["-tA", "-c", sql], timeout=90)
    dims_ok = f"dims={EMBEDDING_DIMENSIONS}" in out
    ext_ok = "ext_version=" in out

    code_trace, out_trace = run(
        psql + ["-tAc", "SELECT count(*) FROM pg_extension WHERE extname='vector';"],
        timeout=90,
    )
    no_trace = code_trace == 0 and out_trace.strip() == "0"

    ok = code == 0 and dims_ok and ext_ok and no_trace
    version = next(
        (line.split("=", 1)[1] for line in out.splitlines() if line.startswith("ext_version=")),
        "?",
    )
    report.add(
        "SVC-03", f"pgvector disponible y acepta vectores de {EMBEDDING_DIMENSIONS} dims",
        "CONTRACT",
        PASS if ok else (SKIP if code != 0 and "Cannot connect" in out else FAIL),
        f"pgvector {version}; dims={EMBEDDING_DIMENSIONS if dims_ok else out.strip()[-120:]}; "
        f"sin rastro tras ROLLBACK={no_trace}",
    )

    if profile:
        status, body = http_get(f"{airflow}/health", timeout=20)
        scheduler_healthy = False
        if status == 200:
            try:
                scheduler_healthy = json.loads(body).get("scheduler", {}).get("status") == "healthy"
            except (json.JSONDecodeError, AttributeError):
                scheduler_healthy = False
        report.add(
            "SVC-04", "orquestación accesible y scheduler activo", "ROBUSTNESS",
            PASS if (status == 200 and scheduler_healthy) else (SKIP if status is None else FAIL),
            f"HTTP {status}, scheduler_healthy={scheduler_healthy}",
        )
    else:
        report.add(
            "SVC-04", "orquestación accesible y scheduler activo", "ROBUSTNESS", SKIP,
            "perfil 'airflow' no activo en esta ejecución",
        )

    status, body = http_get(f"{backend}/health/ready", timeout=30)
    db_reachable = False
    if status is not None:
        try:
            db_reachable = bool(json.loads(body)["database"]["reachable"])
        except (json.JSONDecodeError, KeyError, TypeError):
            db_reachable = False
    report.add(
        "SVC-05", "backend alcanza la base de datos por la red interna", "ROBUSTNESS",
        PASS if db_reachable else (SKIP if status is None else FAIL),
        f"HTTP {status}, database_reachable={db_reachable}",
    )


# ---------------------------------------------------------------------------
# Grupo EMB — modelo de embeddings
# ---------------------------------------------------------------------------
def check_embeddings(report: Report, env: dict[str, str]) -> None:
    backend = f"http://localhost:{env.get('BACKEND_PORT', '8000')}"

    status, body = http_post(f"{backend}/health/embeddings/probe", timeout=900)
    data: dict = {}
    if body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}

    available = data.get("available") is True
    dims = data.get("dimensions")
    title = f"modelo local produce {EMBEDDING_DIMENSIONS} dims y se carga una vez por proceso"

    if status is None:
        report.add("EMB-01", title, "CONTRACT", SKIP, f"backend no alcanzable: {body[:160]}")
    elif not available:
        report.add(
            "EMB-01", title, "CONTRACT", FAIL,
            f"modelo no disponible: {data.get('error') or data.get('detail')}",
        )
    else:
        ok = (
            dims == EMBEDDING_DIMENSIONS
            and data.get("load_count") == 1
            and data.get("loaded_once_per_process") is True
        )
        report.add(
            "EMB-01", title, "CONTRACT", PASS if ok else FAIL,
            f"dimensions={dims} load_count={data.get('load_count')} "
            f"once={data.get('loaded_once_per_process')}",
        )

    is_mount = data.get("cache_dir_is_mount")
    report.add(
        "EMB-02", "caché del modelo en almacenamiento persistente", "ROBUSTNESS",
        PASS if is_mount is True else (SKIP if status is None else FAIL),
        f"cache_dir={data.get('cache_dir')} is_mount={is_mount}",
    )

    source = PRODUCT_ROOT / "backend" / "app" / "embeddings" / "bge_m3.py"
    if source.exists():
        text = source.read_text(encoding="utf-8")
        leaked = [t for t in ("return_sparse", "lexical_weight", "encode_sparse") if t in text]
        report.add(
            "EMB-03", "la recuperación léxica no se delega al modelo", "ROBUSTNESS",
            PASS if not leaked else FAIL,
            "sin API de pesos léxicos en el servicio de embeddings"
            if not leaked else f"API léxica detectada: {leaked}",
        )
    else:
        report.add(
            "EMB-03", "la recuperación léxica no se delega al modelo", "ROBUSTNESS",
            SKIP, "archivo fuente no encontrado",
        )


# ---------------------------------------------------------------------------
# Grupo SCOPE — coherencia del alcance
# ---------------------------------------------------------------------------
def check_scope_static(report: Report) -> None:
    """Comprobaciones de alcance que no requieren el stack en ejecución."""
    app_dir = PRODUCT_ROOT / "backend" / "app"
    business_markers = (
        "rrf", "reciprocal_rank", "bm25", "security_trimming", "cuarentena",
        "jwt", "create_access_token", "oauth", "fragment_id",
    )

    def code_only(path: Path) -> str:
        """Reduce el archivo a tokens de código, descartando comentarios y textos.

        Es imprescindible: los docstrings del módulo de embeddings mencionan BM25
        precisamente para documentar que NO se usa, y un escaneo textual ingenuo lo
        reportaría como lógica de negocio.
        """
        import tokenize

        kept: list[str] = []
        skippable = {
            tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE,
            tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER,
        }
        try:
            with path.open("rb") as handle:
                for token in tokenize.tokenize(handle.readline):
                    if token.type in skippable:
                        continue
                    kept.append(token.string)
        except (tokenize.TokenError, SyntaxError, UnicodeDecodeError):
            # Si no se puede tokenizar, se conserva el texto para no ocultar nada.
            return path.read_text(encoding="utf-8", errors="replace")
        return " ".join(kept)

    hits: list[str] = []
    if app_dir.exists():
        for path in sorted(app_dir.rglob("*.py")):
            code = code_only(path).lower()
            for marker in business_markers:
                if marker in code:
                    hits.append(f"{path.name}:{marker}")

    report.add(
        "SCOPE-01", "el backend no contiene lógica de negocio", "ROBUSTNESS",
        PASS if not hits else FAIL,
        "sin marcadores de lógica de negocio en el código (comentarios y textos excluidos)"
        if not hits else f"detectado en código: {sorted(set(hits))[:8]}",
    )

    _, status = run(["git", "status", "--porcelain"])
    allowed = re.compile(
        r"(\.env\.example|\.gitignore|README\.md|docker-compose\.yml|backend/|frontend/|scripts/|tests/)"
    )
    outside = [
        line for line in status.splitlines()
        if line.strip() and not allowed.search(line)
    ]
    report.add(
        "SCOPE-02", "solo se modifican archivos del repositorio del producto", "ROBUSTNESS",
        PASS if not outside else FAIL,
        "todos los cambios dentro del repositorio"
        if not outside else f"fuera del alcance declarado: {outside[:5]}",
    )


def check_scope_runtime(report: Report, env: dict[str, str]) -> None:
    """La aplicación no debe haber creado estructura de datos."""
    sql = (
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE';"
    )
    code, out = run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U",
         env.get("POSTGRES_USER", "app"), "-d", env.get("POSTGRES_DB", "riesgo_legal"),
         "-tAc", sql],
        timeout=90,
    )
    if code == 0 and out.strip().isdigit():
        tables = int(out.strip())
        report.add(
            "SCOPE-03", "la aplicación no ha creado tablas ni esquemas", "ROBUSTNESS",
            PASS if tables == 0 else FAIL,
            f"tablas en el esquema public = {tables}",
        )
    else:
        report.add(
            "SCOPE-03", "la aplicación no ha creado tablas ni esquemas", "ROBUSTNESS",
            SKIP, f"no se pudo consultar la base de datos (exit={code})",
        )


# ---------------------------------------------------------------------------
# Grupo ENV — capacidad del equipo frente a la configuración de referencia
# ---------------------------------------------------------------------------
def check_environment(report: Report) -> None:
    """Mide la capacidad real del equipo y la compara con la referencia.

    Si el equipo no alcanza la configuración de referencia, se reporta FAIL con la
    medición: es una limitación del entorno de ejecución, no un defecto del
    sistema, y no debe ocultarse ni reinterpretarse la referencia.
    """
    ram_gb: float | None = None
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            ram_gb = stat.ullTotalPhys / (1024 ** 3)
        else:
            meminfo = Path("/proc/meminfo").read_text()
            match = re.search(r"MemTotal:\s+(\d+)", meminfo)
            if match:
                ram_gb = int(match.group(1)) / (1024 ** 2)
    except Exception as exc:  # noqa: BLE001
        report.add(
            "ENV-01", "el equipo alcanza la configuración de referencia", "CONTRACT",
            SKIP, f"no se pudo medir la memoria: {exc.__class__.__name__}",
        )
        return

    disk_gb: float | None = None
    try:
        usage = __import__("shutil").disk_usage(str(PRODUCT_ROOT))
        disk_gb = usage.free / (1024 ** 3)
    except Exception:  # noqa: BLE001
        disk_gb = None

    code, stats = run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}: {{.MemUsage}}"], timeout=90
    )
    footprint = stats if code == 0 else ""

    ram_ok = ram_gb >= REFERENCE_RAM_GB
    disk_ok = disk_gb is not None and disk_gb >= REFERENCE_DISK_GB

    if disk_gb is not None:
        evidence = (
            f"RAM del equipo={ram_gb:.2f}GB (referencia {REFERENCE_RAM_GB:.0f}GB) · "
            f"almacenamiento libre={disk_gb:.2f}GB (referencia {REFERENCE_DISK_GB:.0f}GB)"
        )
    else:
        evidence = f"RAM del equipo={ram_gb:.2f}GB (referencia {REFERENCE_RAM_GB:.0f}GB)"

    report.add(
        "ENV-01", "el equipo alcanza la configuración de referencia", "CONTRACT",
        PASS if (ram_ok and disk_ok) else FAIL,
        evidence,
    )
    report.add(
        "ENV-02", "huella real del stack medida", "ROBUSTNESS",
        PASS if footprint.strip() else SKIP,
        footprint.replace("\n", " | ")[:500] or "(stack no en ejecución)",
    )


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke tests del entorno de desarrollo")
    parser.add_argument("--static-only", action="store_true", help="solo comprobaciones sin Docker")
    parser.add_argument("--skip-runtime", action="store_true", help="omite los servicios en ejecución")
    parser.add_argument("--skip-model", action="store_true", help="omite la comprobación del modelo (descarga pesada)")
    parser.add_argument("--profile", default=None, help="perfil de compose a verificar (p.ej. 'airflow')")
    args = parser.parse_args()

    report = Report()
    env = load_env()

    check_repository(report)
    check_scope_static(report)

    if not args.static_only:
        check_topology(report, args.profile)

        if not args.skip_runtime:
            check_runtime(report, env, args.profile)
            if not args.skip_model:
                check_embeddings(report, env)
            check_scope_runtime(report, env)

        check_environment(report)

    print(report.render())
    return 1 if report.counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
