"""Framework / reasoner boundary enforcement.

DecisionLedger is a domain-agnostic governance framework. ``app/`` (the
framework runtime) and ``core/`` (the framework contracts) must not
import from any reasoner package, with one exception: ``app/main.py`` is
the deployment composer and may import a reasoner's router and any
factory it needs to wire shared infrastructure into reasoner services.

This test codifies that rule. Any new reasoner-specific symbol that
leaks into framework code or any new framework→reasoner import sites
will fail this test on the next ``make check``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
CORE_DIR = REPO_ROOT / "core"
DEPLOYMENT_SEAM = APP_DIR / "main.py"

REASONER_IMPORT = re.compile(r"^\s*(?:from|import)\s+reasoner\.", re.MULTILINE)
APP_IMPORT = re.compile(r"^\s*(?:from|import)\s+app(?:\.|\s|$)", re.MULTILINE)

# ATO-coded symbols that must not appear in framework code. Each has at
# most one location — the reasoner's own package.
ATO_SYMBOLS = re.compile(
    r"\b(LoginEvent|AtoFeatureVector|AtoScorer|ScorerOutput|AuthOutcome|"
    r"AuthMethod|account_takeover\.policy_chunks|account_takeover\.decision_bundles)\b"
)


def _py_files(root: Path) -> list[Path]:
    """Return every .py file under root, skipping caches."""
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def test_app_does_not_import_reasoner_except_main():
    """Framework code must not import from any reasoner package.

    The single exception is ``app/main.py`` — it is the deployment
    composer and is allowed to mount reasoner routers and supply
    reasoner-specific factories (e.g., the raw_event deserializer) at
    lifespan startup.
    """
    offenders = []
    for path in _py_files(APP_DIR):
        if path == DEPLOYMENT_SEAM:
            continue
        text = path.read_text(encoding="utf-8")
        if REASONER_IMPORT.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "framework code (app/) must not import from reasoner.* "
        f"(deployment seam app/main.py excepted): {offenders}"
    )


def test_core_does_not_import_app_or_reasoner():
    """``core/`` is the framework's pure-types layer.

    It must not depend on anything in ``app/`` (framework runtime) or
    ``reasoner/`` (domain implementations). The whole point of ``core/``
    is to be importable from any test, generator, or future reasoner
    without dragging in infrastructure clients.
    """
    offenders = []
    for path in _py_files(CORE_DIR):
        text = path.read_text(encoding="utf-8")
        if APP_IMPORT.search(text) or REASONER_IMPORT.search(text):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"core/ must not import from app.* or reasoner.*: {offenders}"


def test_no_ato_symbols_in_framework_code():
    """Concrete ATO-domain symbols must not appear in framework source.

    A leak typically looks like a type annotation (``LoginEvent``), an
    enum check (``AuthOutcome.FAILURE``), or a hardcoded schema name
    (``account_takeover.policy_chunks``). Every such symbol belongs in
    the reasoner package; the framework should type on protocols and
    framework-owned schema names.
    """
    offenders: list[tuple[str, str]] = []
    for path in _py_files(APP_DIR):
        if path == DEPLOYMENT_SEAM:
            continue
        text = path.read_text(encoding="utf-8")
        match = ATO_SYMBOLS.search(text)
        if match is not None:
            offenders.append((str(path.relative_to(REPO_ROOT)), match.group(0)))
    assert not offenders, (
        "framework code references ATO-coded symbols "
        "(domain implementation belongs in reasoner/account_takeover/): "
        f"{offenders}"
    )
