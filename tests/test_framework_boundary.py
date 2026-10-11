"""Framework / reasoner boundary enforcement.

DecisionLedger is a domain-agnostic governance framework. ``app/`` (the
framework runtime), ``core/`` (the framework contracts), and ``eval/`` (the
generic evaluation harness) must not import from any reasoner package and
must not name any reasoner's symbols, ids, templates, features, or
jurisdictions — not even in docstrings, comments, or log strings. Composition
happens purely through plugin discovery (``decisionledger.reasoners`` entry
points), so a new reasoner is a new installed package, never a framework edit.

Three scans and two build-metadata checks codify that rule:

- **Import scan** over every ``.py`` under ``app/``, ``core/``, ``eval/``:
  no ``from``/``import`` of ``reasoner``, ``reasoners``, or ``ato_reasoner``
  (bare ``import reasoner`` included). ``ato_reasoner`` as a token anywhere
  is caught by the token scan.
- **Token scan** over ``.py``, ``.yaml``, and ``.sql`` under the same trees
  plus the packaged Elasticsearch mapping (``app/retrieval/es/*.json``): no
  ATO-coded identifier, hyphenated id, template id, feature name, or
  jurisdiction tag. Nothing is allowlisted; a leak is fixed by rewording.
- **Root build metadata** (``pyproject.toml`` via ``tomllib``): the framework
  distribution carries no reasoner dependencies, declares no entry points of
  its own, and ships only ``core``, ``app``, ``eval``.
- **Member build metadata**: each reasoner workspace member under
  ``reasoners/*`` declares its runtime plugin and eval adapter entry points
  itself, pointing into its own import package.

Any new reasoner-specific symbol that leaks into framework code or any new
framework→reasoner import site fails this test on the next ``make check``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
CORE_DIR = REPO_ROOT / "core"
EVAL_DIR = REPO_ROOT / "eval"
ES_DIR = APP_DIR / "retrieval" / "es"
REASONERS_DIR = REPO_ROOT / "reasoners"
PYPROJECT = REPO_ROOT / "pyproject.toml"

FRAMEWORK_DIRS = (APP_DIR, CORE_DIR, EVAL_DIR)

# An import statement whose first dotted component is a reasoner package.
# ``(?:\.|\s|$)`` after the name catches bare ``import reasoner`` as well as
# ``from reasoner.x import y`` and ``import reasoners.x as z``.
REASONER_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(?:reasoner|reasoners|ato_reasoner)(?:\.|\s|$)",
    re.MULTILINE,
)
APP_IMPORT = re.compile(r"^\s*(?:from|import)\s+app(?:\.|\s|$)", re.MULTILINE)

# ATO-coded tokens that must not appear in framework code or config. Each has
# exactly one home — the ATO reasoner's own package.
ATO_TOKENS = re.compile(
    r"\b(LoginEvent|AtoFeatureVector|AtoScorer|AtoSettings|FeatureService|"
    r"run_ato_decision|AuthOutcome|AuthMethod|account_takeover|ato_reasoner|"
    r"ato-reasoner|ato-v\d|sparse_history|US_FEDERAL|US_STATE|EU_GDPR|"
    r"login[_ ]event)\b"
)
ATO_WORD = re.compile(r"\bATO\b", re.IGNORECASE)


def _py_files(root: Path) -> list[Path]:
    """Return every .py file under root, skipping caches."""
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _scan_files() -> list[Path]:
    """Every .py, .yaml, and .sql under the framework trees plus the ES mapping."""
    files: list[Path] = []
    for root in FRAMEWORK_DIRS:
        files.extend(_py_files(root))
        files.extend(root.rglob("*.yaml"))
        files.extend(root.rglob("*.yml"))
        files.extend(root.rglob("*.sql"))
    files.extend(ES_DIR.glob("*.json"))
    return sorted(p for p in files if "__pycache__" not in p.parts)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# ---------------------------------------------------------------------------
# Import scans
# ---------------------------------------------------------------------------


def test_framework_code_does_not_import_reasoner_packages():
    """No ``.py`` under app/, core/, or eval/ imports a reasoner package.

    There is no exception for ``app/main.py``: it composes reasoners by
    discovering ``ReasonerPlugin`` objects through entry points.
    """
    offenders = []
    for root in FRAMEWORK_DIRS:
        for path in _py_files(root):
            text = path.read_text(encoding="utf-8")
            if REASONER_IMPORT.search(text):
                offenders.append(_rel(path))
    assert not offenders, (
        "framework code must not import from reasoner.* / reasoners.* / "
        f"ato_reasoner: {offenders}"
    )


def test_core_does_not_import_app_or_reasoner():
    """``core/`` is the framework's pure-types layer.

    It must not depend on anything in ``app/`` (framework runtime) or any
    reasoner package. The whole point of ``core/`` is to be importable from
    any test, generator, or future reasoner without dragging in
    infrastructure clients.
    """
    offenders = []
    for path in _py_files(CORE_DIR):
        text = path.read_text(encoding="utf-8")
        if APP_IMPORT.search(text) or REASONER_IMPORT.search(text):
            offenders.append(_rel(path))
    assert not offenders, f"core/ must not import from app.* or reasoner.*: {offenders}"


# ---------------------------------------------------------------------------
# Token scan
# ---------------------------------------------------------------------------


def test_no_ato_tokens_in_framework_code_or_config():
    """No ATO-coded token appears in framework source, YAML, or the ES mapping.

    A leak typically looks like a type annotation (``LoginEvent``), an enum
    check (``AuthOutcome.FAILURE``), a hardcoded tenant (``ato-reasoner``),
    a feature name (``sparse_history``), a jurisdiction (``US_FEDERAL``), or
    a docstring example that says "ATO". Every such token belongs in the
    reasoner package; the framework speaks only in protocols and generic
    examples.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted(
            {m.group(0) for m in ATO_TOKENS.finditer(text)}
            | {m.group(0) for m in ATO_WORD.finditer(text)}
        )
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, (
        "framework code references ATO-coded tokens (domain vocabulary belongs "
        f"in the reasoner package): {offenders}"
    )


# ---------------------------------------------------------------------------
# Build metadata — the framework distribution carries no reasoner
# ---------------------------------------------------------------------------

_REASONER_DEPS = re.compile(r"^(xgboost|pandas|redis|sqlalchemy|alembic)\b", re.I)


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_root_dependencies_carry_no_reasoner_infrastructure():
    """xgboost/pandas/redis/sqlalchemy/alembic are ATO deps, not framework deps."""
    deps = _pyproject()["project"]["dependencies"]
    offenders = [d for d in deps if _REASONER_DEPS.match(d.strip())]
    assert not offenders, f"root project.dependencies must not include {offenders}"


def test_root_package_declares_no_entry_points():
    """Reasoner entry points belong to reasoner distributions, not the framework."""
    project = _pyproject()["project"]
    assert "entry-points" not in project, (
        "root pyproject declares [project.entry-points]: "
        f"{sorted(project['entry-points'])}"
    )


def test_root_wheel_ships_only_framework_packages():
    """The framework wheel is exactly core, app, eval."""
    packages = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(packages) == {"core", "app", "eval"}, packages


def test_es_mapping_ships_inside_the_framework_package():
    """The token scan must actually see the packaged ES mapping."""
    assert sorted(p.name for p in ES_DIR.glob("*.json")) == ["policy-chunks.json"]


def _member_pyprojects() -> list[Path]:
    return sorted(REASONERS_DIR.glob("*/pyproject.toml"))


def test_every_reasoner_member_declares_its_own_entry_points():
    """Runtime plugin and eval adapter entry points live on the member.

    Each ``reasoners/<domain>/pyproject.toml`` declares one
    ``decisionledger.reasoners`` and one ``decisionledger.eval_adapters``
    entry, and both targets resolve inside the member's own import package
    (the single package its wheel ships).
    """
    members = _member_pyprojects()
    assert members, "no reasoner workspace members found under reasoners/*"
    problems: list[str] = []
    for pyproject in members:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        rel = _rel(pyproject)
        wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        if len(wheel) != 1 or not wheel[0].startswith("src/"):
            problems.append(f"{rel}: wheel must ship exactly one src/ package")
            continue
        package = wheel[0].removeprefix("src/")
        eps = data["project"].get("entry-points", {})
        for group in ("decisionledger.reasoners", "decisionledger.eval_adapters"):
            entries = eps.get(group, {})
            if len(entries) != 1:
                problems.append(f"{rel}: expected one {group} entry, got {entries}")
                continue
            (target,) = entries.values()
            if not target.startswith(f"{package}."):
                problems.append(f"{rel}: {group} -> {target} is outside {package}")
    assert not problems, problems
