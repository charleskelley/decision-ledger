"""Framework / reasoner / gate boundary enforcement.

DecisionLedger is a domain-agnostic governance framework. ``app/`` (the
framework runtime), ``core/`` (the framework contracts), and ``eval/`` (the
generic evaluation harness) must not import from any reasoner package and
must not name any reasoner's symbols, ids, templates, features, or
jurisdictions — not even in docstrings, comments, or log strings. Composition
happens purely through plugin discovery (``decisionledger.reasoners`` entry
points), so a new reasoner is a new installed package, never a framework edit.

Gates follow the same rule (DR-28). A gate is discovered through the
``decisionledger.gates`` entry-point group, and the framework stores the
universal ``GateOutput`` base plus the gate's opaque extension
(``evidence_snapshot``, ``extra``); it never defines or imports a gate's
``GateOutput`` / ``GateInput`` subclass and never names a gate member.

Scans and build-metadata checks codify those rules:

- **Import scan** over every ``.py`` under ``app/``, ``core/``, ``eval/``:
  no ``from``/``import`` of ``reasoner``, ``reasoners``, or ``ato_reasoner``
  (bare ``import reasoner`` included), none of a gate member
  (``rules_gate``, ``policy_rag_gate``), and nothing from ``tests`` (the
  stub reasoner and the fake gate are test doubles, never framework code).
- **Token scan** over ``.py``, ``.yaml``, and ``.sql`` under the same trees
  plus the packaged Elasticsearch mapping (``app/retrieval/es/*.json``): no
  ATO-coded identifier, hyphenated id, template id, feature name, or
  jurisdiction tag, and no gate-member vocabulary. Nothing is allowlisted; a
  leak is fixed by rewording.
- **Seam scans**: the framework's generic modules neither name the
  reference policy gate's internals (retriever, prompt registry, retrieval
  layers, LLM adapters) nor import its modules under any name. Until Phase 1b
  moves that gate into its own workspace member, its interim homes inside
  the framework trees are the only exemptions, plus two eval files that wire
  its eval dimensions (per-file allowances). Every exemption must still be
  needed, so a stale one fails.
- **Gate subclass scan** (AST): no framework module defines or imports a
  ``GateOutput`` or ``GateInput`` subclass, aliased bases included.
- **Root build metadata** (``pyproject.toml`` via ``tomllib``): the framework
  distribution carries no reasoner or gate-member dependencies, declares no
  entry points of its own except the interim ``decisionledger.gates`` entry
  for the reference policy gate (removed in Phase 1b), and ships only
  ``core``, ``app``, ``eval``.
- **Member build metadata**: each reasoner workspace member under
  ``reasoners/*`` declares its runtime plugin and eval adapter entry points
  itself, and each gate member under ``gates/*`` declares its gate entry
  point itself, pointing into its own import package. Gate members are
  reasoner-agnostic: no reasoner imports and no ATO tokens.

Any new reasoner- or gate-specific symbol that leaks into framework code or
any new framework→plugin import site fails this test on the next
``make check``.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
CORE_DIR = REPO_ROOT / "core"
EVAL_DIR = REPO_ROOT / "eval"
ES_DIR = APP_DIR / "retrieval" / "es"
REASONERS_DIR = REPO_ROOT / "reasoners"
GATES_DIR = REPO_ROOT / "gates"
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

# An import of a gate workspace member or of anything under tests/ (the stub
# reasoner and the fake gate are test doubles).
GATE_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(?:rules_gate|policy_rag_gate|gates|tests)(?:\.|\s|$)",
    re.MULTILINE,
)

# ATO-coded tokens that must not appear in framework code or config. Each has
# exactly one home — the ATO reasoner's own package.
ATO_TOKENS = re.compile(
    r"\b(LoginEvent|AtoFeatureVector|AtoScorer|AtoSettings|FeatureService|"
    r"run_ato_decision|AuthOutcome|AuthMethod|account_takeover|ato_reasoner|"
    r"ato-reasoner|ato-v\d|sparse_history|US_FEDERAL|US_STATE|EU_GDPR|"
    r"login[_ ]event)\b"
)
ATO_WORD = re.compile(r"\bATO\b", re.IGNORECASE)

# Gate-member vocabulary that must not appear in framework code or config,
# matched case-insensitively by prefix: the rules gate's package, settings
# (RULES_GATE_*), types, and rule-set vocabulary; any gate distribution name;
# the policy gate's future member package; the test-only fake gate.
GATE_MEMBER_TOKENS = re.compile(
    r"\b(rules_gate\w*|rulesets?\w*|decisionledger-gate-[\w-]+|"
    r"policy_rag_gate\w*|_fake_gate\w*|fake_?gate\w*)",
    re.IGNORECASE,
)

# The reference policy gate's internals. The framework's generic modules
# (bundle, enforcement, dispatch, bootstrap, plugin seams, audit store) must
# not name any of them: the gate owns its retriever, prompts, LLM client, and
# retrieval record (DR-28).
POLICY_GATE_TOKENS = re.compile(
    r"\b(PolicyGate\w*|GateResult|PolicyRetriever|RetrievedSnippet|"
    r"retrieval_query|retrieval_results|retrieval_path|index_version|"
    r"corpus_version|prompt_registry|PromptRegistry|YamlPromptRegistry|"
    r"Elasticsearch|SentenceTransformer|CrossEncoder|OpenAILLMClient|"
    r"AnthropicLLMClient|register_vector)\b"
)

# Interim homes of the reference policy gate inside the framework trees. Each
# entry leaves the framework when Phase 1b moves the gate (its runtime,
# retrieval, LLM adapters, typed models, and eval dimensions) into
# ``gates/policy_rag/`` (directories end with ``/``).
POLICY_GATE_INTERIM_HOMES = (
    "app/gate/policy/",
    "app/retrieval/",
    "app/llm/",
    "core/gate/policy/",
    "core/snippet.py",
    "eval/clients/",
    "eval/dimensions/citation.py",
    "eval/dimensions/faithfulness.py",
    "eval/dimensions/retrieval.py",
    "eval/judge.py",
)

#: The policy gate's modules: no framework module outside the interim homes
#: imports any of them (or anything below them).
POLICY_GATE_MODULES = (
    "app.gate.policy",
    "app.llm",
    "app.retrieval",
    "core.gate.policy",
    "core.snippet",
    "eval.clients",
    "eval.dimensions.citation",
    "eval.dimensions.faithfulness",
    "eval.dimensions.retrieval",
    "eval.judge",
)

#: Framework-permanent files that still wire the policy gate's eval until
#: Phase 1b moves those dimensions into the gate member: the harness builds
#: the LLM judge for faithfulness and citation and runs the retrieval
#: dimension; the eval adapter's driver protocol exposes the retriever that
#: dimension reads. Each allowance names exactly what the file may use.
POLICY_GATE_TOKEN_ALLOWANCES: dict[str, frozenset[str]] = {
    "eval/runners/harness.py": frozenset({"AnthropicLLMClient"}),
}
POLICY_GATE_IMPORT_ALLOWANCES: dict[str, frozenset[str]] = {
    "eval/runners/harness.py": frozenset(
        {
            "app.llm",
            "eval.clients",
            "eval.dimensions.citation",
            "eval.dimensions.faithfulness",
            "eval.dimensions.retrieval",
        }
    ),
    "eval/adapter.py": frozenset({"eval.dimensions.retrieval"}),
}

# Gate contracts whose subclasses the framework must never define or import.
# ``GateVerdict`` is deliberately absent: the policy gate's structured-output
# schema (``PolicyGateVerdict``) still lives in ``core/gate/policy/`` until
# Phase 1b, and the bundle stores only the universal base either way.
CLOSED_GATE_BASES = frozenset({"GateOutput", "GateInput"})


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


def _is_policy_gate_interim_home(path: Path) -> bool:
    rel = _rel(path)
    return any(
        rel.startswith(home) if home.endswith("/") else rel == home
        for home in POLICY_GATE_INTERIM_HOMES
    )


def _policy_gate_modules_imported(path: Path) -> set[str]:
    """Policy-gate modules (``POLICY_GATE_MODULES`` entries) ``path`` imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
            imported.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return {
        module
        for name in imported
        for module in POLICY_GATE_MODULES
        if name == module or name.startswith(f"{module}.")
    }


def _gate_member_src_files() -> list[Path]:
    """Every .py and .yaml under each gate member's ``src/`` tree."""
    files: list[Path] = []
    for src in sorted(GATES_DIR.glob("*/src")):
        files.extend(_py_files(src))
        files.extend(src.rglob("*.yaml"))
    return sorted(files)


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


def test_framework_code_does_not_import_gate_members_or_tests():
    """No ``.py`` under app/, core/, or eval/ imports a gate member or tests.

    Gates are composed by discovering ``GatePlugin`` objects through the
    ``decisionledger.gates`` entry-point group; the test-only stub reasoner
    and fake gate are injected by tests, never imported by the framework.
    """
    offenders = []
    for root in FRAMEWORK_DIRS:
        for path in _py_files(root):
            text = path.read_text(encoding="utf-8")
            if GATE_IMPORT.search(text):
                offenders.append(_rel(path))
    assert not offenders, (
        "framework code must not import a gate member (rules_gate, "
        f"policy_rag_gate) or anything under tests/: {offenders}"
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
# Token scans
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


def test_no_gate_member_tokens_in_framework_code_or_config():
    """No gate member's vocabulary appears in framework source or config.

    The rules gate's package and types, the policy gate's future member
    names, and the fake gate's names belong to those packages; the framework
    knows gates only through ``GatePlugin`` / ``Gate`` and the universal
    ``GateOutput``.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in GATE_MEMBER_TOKENS.finditer(text)})
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, f"framework code references gate-member tokens: {offenders}"


def test_generic_framework_modules_do_not_name_policy_gate_internals():
    """The bundle, enforcement, dispatch, and plugin seams are gate-agnostic.

    Retrieval layers, the retriever, the prompt registry, and the LLM
    adapters are the reference policy gate's own machinery (DR-28). Only the
    gate's interim homes (``POLICY_GATE_INTERIM_HOMES``) may name them, and
    a few eval files may name exactly the tokens they are allowed; every
    other framework module reaches a gate through ``GateRequest`` and
    ``GateOutput`` alone.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        if _is_policy_gate_interim_home(path):
            continue
        allowed = POLICY_GATE_TOKEN_ALLOWANCES.get(_rel(path), frozenset())
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in POLICY_GATE_TOKENS.finditer(text)} - allowed)
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, (
        "generic framework modules name the policy gate's internals (they "
        f"belong to the gate): {offenders}"
    )


def test_generic_framework_modules_do_not_import_the_policy_gate():
    """No generic module imports a policy-gate module, under any name.

    A token scan cannot see ``from app.retrieval import retriever as r``;
    this AST scan can. ``import core.gate`` in particular must not load the
    policy gate's models, so the universal contract package does not
    re-export them.
    """
    offenders: list[tuple[str, list[str]]] = []
    for root in FRAMEWORK_DIRS:
        for path in _py_files(root):
            if _is_policy_gate_interim_home(path):
                continue
            allowed = POLICY_GATE_IMPORT_ALLOWANCES.get(_rel(path), frozenset())
            hits = sorted(_policy_gate_modules_imported(path) - allowed)
            if hits:
                offenders.append((_rel(path), hits))
    assert not offenders, (
        f"generic framework modules import policy-gate modules: {offenders}"
    )


def test_every_policy_gate_exemption_is_still_needed():
    """Each interim home still holds the policy gate, and each allowance is used.

    An exemption that nothing needs any more (the gate moved, a reference was
    reworded) fails here, so the list shrinks to nothing as Phase 1b moves
    the gate instead of silently exempting framework-permanent code.
    """
    stale: list[str] = []
    for home in POLICY_GATE_INTERIM_HOMES:
        target = REPO_ROOT / home
        files = _py_files(target) if home.endswith("/") else [target]
        if not files or not any(
            POLICY_GATE_TOKENS.search(f.read_text(encoding="utf-8"))
            or _policy_gate_modules_imported(f)
            for f in files
        ):
            stale.append(f"interim home {home}")
    for rel, tokens in POLICY_GATE_TOKEN_ALLOWANCES.items():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        found = {m.group(0) for m in POLICY_GATE_TOKENS.finditer(text)}
        stale.extend(f"token {t} in {rel}" for t in sorted(tokens - found))
    for rel, modules in POLICY_GATE_IMPORT_ALLOWANCES.items():
        imported = _policy_gate_modules_imported(REPO_ROOT / rel)
        stale.extend(f"import {m} in {rel}" for m in sorted(modules - imported))
    assert not stale, f"stale policy-gate exemptions: {stale}"


# ---------------------------------------------------------------------------
# Gate subclass scan — the framework stores the universal base only
# ---------------------------------------------------------------------------


def _closed_base_aliases(tree: ast.AST) -> set[str]:
    """Names bound to ``GateOutput``/``GateInput`` in a module.

    Covers the plain names, ``from ... import GateOutput as X``, and simple
    re-bindings such as ``X = GateOutput`` (followed transitively).
    """
    aliases = set(CLOSED_GATE_BASES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            aliases.update(
                alias.asname
                for alias in node.names
                if alias.name in CLOSED_GATE_BASES and alias.asname
            )
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in aliases:
                        aliases.add(target.id)
                        changed = True
    return aliases


def _subclasses_a_closed_base(node: ast.ClassDef, aliases: set[str]) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in aliases:
            return True
        if isinstance(base, ast.Attribute) and base.attr in CLOSED_GATE_BASES:
            return True
    return False


def _is_closed_gate_subclass_name(name: str) -> bool:
    return any(name != base and name.endswith(base) for base in CLOSED_GATE_BASES)


def test_framework_never_defines_or_imports_a_gate_output_or_input_subclass():
    """DR-28: the closed discriminated union of DR-20 is gone.

    A ``GateOutput`` / ``GateInput`` subclass would be a gate's private
    schema; the framework persists and replays the universal base plus the
    opaque ``evidence_snapshot`` / ``extra``, so it neither defines such a
    subclass nor imports one (``PolicyGateOutput``-style names included).
    """
    offenders: list[str] = []
    for root in FRAMEWORK_DIRS:
        for path in _py_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = _closed_base_aliases(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and _subclasses_a_closed_base(
                    node, aliases
                ):
                    offenders.append(f"{_rel(path)}: defines {node.name}")
                elif isinstance(node, ast.ImportFrom):
                    offenders.extend(
                        f"{_rel(path)}: imports {alias.name}"
                        for alias in node.names
                        if _is_closed_gate_subclass_name(alias.name)
                    )
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# Build metadata — the framework distribution carries no reasoner or gate
# ---------------------------------------------------------------------------

_REASONER_DEPS = re.compile(r"^(xgboost|pandas|redis|sqlalchemy|alembic)\b", re.I)
_GATE_MEMBER_DEPS = re.compile(r"^decisionledger-gate-", re.I)

#: The one entry point the root package may declare in Phase 1a: the reference
#: policy gate, registered through the plugin protocol while its code still
#: lives in ``app/gate/policy``. Phase 1b moves it to its own member and
#: deletes this entry (and this allowance).
_INTERIM_ROOT_ENTRY_POINTS = {
    "decisionledger.gates": {"policy": "app.gate.policy.plugin:PLUGIN"},
}


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_root_dependencies_carry_no_reasoner_infrastructure():
    """xgboost/pandas/redis/sqlalchemy/alembic are ATO deps, not framework deps."""
    deps = _pyproject()["project"]["dependencies"]
    offenders = [d for d in deps if _REASONER_DEPS.match(d.strip())]
    assert not offenders, f"root project.dependencies must not include {offenders}"


def test_root_dependencies_carry_no_gate_member():
    """Gate members are composed by a dependency group, never required."""
    deps = _pyproject()["project"]["dependencies"]
    offenders = [d for d in deps if _GATE_MEMBER_DEPS.match(d.strip())]
    assert not offenders, f"root project.dependencies must not include {offenders}"


def test_root_package_declares_only_the_interim_policy_gate_entry_point():
    """Reasoner and gate entry points belong to their own distributions.

    The single exception is the interim policy-gate entry (DR-28, Phase 1a),
    mirroring DR-26 Phase 1; no reasoner or eval-adapter group may appear.
    """
    project = _pyproject()["project"]
    assert project.get("entry-points", {}) == _INTERIM_ROOT_ENTRY_POINTS, (
        "root pyproject [project.entry-points] must be exactly the interim "
        f"policy gate entry: {project.get('entry-points')}"
    )


def test_root_wheel_ships_only_framework_packages():
    """The framework wheel is exactly core, app, eval."""
    packages = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(packages) == {"core", "app", "eval"}, packages


def test_es_mapping_ships_inside_the_framework_package():
    """The token scan must actually see the packaged ES mapping."""
    assert sorted(p.name for p in ES_DIR.glob("*.json")) == ["policy-chunks.json"]


def _member_pyprojects(root: Path) -> list[Path]:
    return sorted(root.glob("*/pyproject.toml"))


def _member_package(data: dict) -> str | None:
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    if len(wheel) != 1 or not wheel[0].startswith("src/"):
        return None
    return wheel[0].removeprefix("src/")


def test_every_reasoner_member_declares_its_own_entry_points():
    """Runtime plugin and eval adapter entry points live on the member.

    Each ``reasoners/<domain>/pyproject.toml`` declares one
    ``decisionledger.reasoners`` and one ``decisionledger.eval_adapters``
    entry, and both targets resolve inside the member's own import package
    (the single package its wheel ships).
    """
    members = _member_pyprojects(REASONERS_DIR)
    assert members, "no reasoner workspace members found under reasoners/*"
    problems: list[str] = []
    for pyproject in members:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        rel = _rel(pyproject)
        package = _member_package(data)
        if package is None:
            problems.append(f"{rel}: wheel must ship exactly one src/ package")
            continue
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


def test_every_gate_member_declares_its_own_gate_entry_point():
    """Each ``gates/<name>/pyproject.toml`` publishes exactly one gate.

    The ``decisionledger.gates`` entry targets the member's own import
    package, and a gate member declares no reasoner or eval-adapter entry
    point.
    """
    members = _member_pyprojects(GATES_DIR)
    assert members, "no gate workspace members found under gates/*"
    problems: list[str] = []
    for pyproject in members:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        rel = _rel(pyproject)
        package = _member_package(data)
        if package is None:
            problems.append(f"{rel}: wheel must ship exactly one src/ package")
            continue
        eps = data["project"].get("entry-points", {})
        if set(eps) != {"decisionledger.gates"}:
            problems.append(f"{rel}: entry-point groups must be exactly gates: {eps}")
            continue
        entries = eps["decisionledger.gates"]
        if len(entries) != 1:
            problems.append(
                f"{rel}: expected one decisionledger.gates entry: {entries}"
            )
            continue
        (target,) = entries.values()
        if not target.startswith(f"{package}."):
            problems.append(
                f"{rel}: decisionledger.gates -> {target} is outside {package}"
            )
    assert not problems, problems


def test_gate_members_are_reasoner_agnostic():
    """A gate serves every reasoner: no reasoner imports, no ATO vocabulary."""
    offenders: list[tuple[str, list[str]]] = []
    for path in _gate_member_src_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted(
            {m.group(0) for m in ATO_TOKENS.finditer(text)}
            | {m.group(0) for m in ATO_WORD.finditer(text)}
        )
        if REASONER_IMPORT.search(text):
            hits.append("<reasoner import>")
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, f"gate members reference reasoners: {offenders}"
