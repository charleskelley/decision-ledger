"""Import boundary enforcement — DR-11.

Framework subpackages (core/observation/, core/gate/, core/eval/) and top-level
framework modules (core/bundle.py, core/actions.py, core/exceptions.py) must
never import from domain subpackages (reasoner/ or any future domain package).
The dependency arrow points inward only: subpackages → top-level → (nothing in core/).

This test walks the AST of every framework module and fails if any import from
a domain subpackage is found. It is intentionally fast — no Docker, no I/O
beyond reading .py files — and runs on every `make test` invocation.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

FRAMEWORK_DIRS = [
    REPO_ROOT / "core" / "observation",
    REPO_ROOT / "core" / "gate",
    REPO_ROOT / "core" / "eval",
]

FRAMEWORK_TOP_LEVEL = [
    REPO_ROOT / "core" / "actions.py",
    REPO_ROOT / "core" / "bundle.py",
    REPO_ROOT / "core" / "exceptions.py",
]

DOMAIN_PREFIXES = [
    "reasoner",
]


def _collect_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def test_framework_modules_do_not_import_from_domain_subpackages():
    violations = []
    sources: list[Path] = list(FRAMEWORK_TOP_LEVEL)
    for directory in FRAMEWORK_DIRS:
        sources.extend(sorted(directory.glob("*.py")))

    for py_file in sources:
        if not py_file.exists():
            continue
        for imported in _collect_imports(py_file):
            for domain_prefix in DOMAIN_PREFIXES:
                if imported.startswith(domain_prefix):
                    relative = py_file.relative_to(REPO_ROOT)
                    violations.append(f"  {relative}: imports {imported}")

    assert not violations, (
        "DR-11 violation — framework modules must not import from domain subpackages:\n"
        + "\n".join(violations)
    )


def test_gate_subpackage_does_not_import_from_observation_subpackage():
    # core/gate/ depends only on core/actions.py — not on core/observation/.
    # Subpackages must not import laterally from each other.
    violations = []
    gate_dir = REPO_ROOT / "core" / "gate"
    for py_file in sorted(gate_dir.glob("*.py")):
        for imported in _collect_imports(py_file):
            if imported.startswith("core.observation"):
                relative = py_file.relative_to(REPO_ROOT)
                violations.append(f"  {relative}: imports {imported}")

    assert not violations, (
        "DR-11 violation — core/gate/ must not import from core/observation/:\n"
        + "\n".join(violations)
    )


def test_observation_subpackage_does_not_import_from_gate_subpackage():
    # core/observation/ must not import from core/gate/ — gate types are
    # assembled by bundle.py, not by the intake contract layer.
    violations = []
    obs_dir = REPO_ROOT / "core" / "observation"
    for py_file in sorted(obs_dir.glob("*.py")):
        for imported in _collect_imports(py_file):
            if imported.startswith("core.gate"):
                relative = py_file.relative_to(REPO_ROOT)
                violations.append(f"  {relative}: imports {imported}")

    assert not violations, (
        "DR-11 violation — core/observation/ must not import from core/gate/:\n"
        + "\n".join(violations)
    )
