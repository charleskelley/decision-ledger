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
``GateOutput`` / ``GateInput`` / ``GateVerdict`` subclass and never names a
gate member. Since Phase 1b the reference policy gate is a workspace member
(``gates/policy_rag``) like every other gate: its retrieval stack, LLM
adapters, typed models, schema, and eval dimensions left the framework, and
the framework's only infrastructure is Postgres.

Scans and build-metadata checks codify those rules:

- **Import scan** over every ``.py`` under ``app/``, ``core/``, ``eval/``:
  no ``from``/``import`` of ``reasoner``, ``reasoners``, or ``ato_reasoner``
  (bare ``import reasoner`` included), none of a gate member
  (``rules_gate``, ``policy_rag_gate``), and nothing from ``tests`` (the
  stub reasoner and the fake gate are test doubles, never framework code).
  An AST scan repeats it for every workspace member's package, read from
  the members' pyprojects, so a member added later is covered too.
- **LLM SDK confinement** (DR-23, AST): ``openai`` and ``anthropic`` are
  imported only by the RAG policy gate's ``llm/`` adapters, in every tree.
- **Token scan** over ``.py``, ``.yaml``, ``.sql``, and ``.json`` under the
  same trees: no ATO-coded identifier, hyphenated id, template id, feature
  name, or jurisdiction tag; no gate-member vocabulary; and no name of the
  infrastructure a gate owns (search index, vector extension, embedding,
  LLM SDK, or eval-scorer packages, and the chunk table), inside
  identifiers too. One identifier is excepted, the ``EvalReport`` schema
  key ``ragas_faithfulness``, and the exception must stay needed; any
  other leak is fixed by rewording.
- **Seam scans**: no framework module names the reference policy gate's
  internals (retriever, prompt registry, retrieval record, LLM adapters),
  with no exemptions, and none of the gate's former homes inside the
  framework trees still holds a file.
- **Submission contract** (DR-29, DR-30, DR-31): the in-process
  ``Observation`` handoff is gone. No framework file names the protocol, its
  validator and error, the gate route and its members, the fast-path
  rationale, the per-reasoner raw-event deserializer, or the old
  orchestrator, the old assembler entry point, or the registration's old
  gate allow-list, and none says "fast path" in any spelling, inside an
  identifier included (a reasoner either records or asks for a gate). No
  member's source (code, YAML, SQL, JSON) names the removed identifiers
  either, and the handoff's former homes (``core/routes.py``,
  ``core/observation/observation.py``, ``app/decide.py``) hold no file. One
  committed training artifact (a model card's band metrics) keeps the old
  band names, as a listed exception that must stay needed. Member Markdown
  is not scanned: the reference policy corpus is indexed content, and its
  documents may name the bands of their time.
- **Ledger API** (DR-31, Phase 3): the framework no longer hosts reasoners.
  No framework file mounts a plugin's router, builds a reasoner's
  request-time services, or keeps them on ``app.state``; reasoners run as
  their own services or in process through ``app.client.LocalClient``. No
  framework file names any workspace reasoner (package, distribution, or
  ``reasoner_id``, read from the members' pyprojects). The conformance suite
  is a black box: it reaches an instance over HTTP only and imports no
  framework, member, or test code. Integration members (``integrations/*``)
  reach the ledger through ``app.client`` only and publish no plugin.
- **Identity contract** (DR-32, Phase 3b): the framework assigns every
  decision a resolved subject through a resolver the enterprise supplies,
  and implements no identity resolution itself. No framework file has a
  household concept (relationship types are a resolver's, declared when it
  is composed) or names the test-only stub resolver, and no member still
  keeps a private subject namespace constant (a former one by name, or any
  module- or class-level ``UUID(...)`` binding in a reasoner member): a
  reasoner declares its native namespaces in its registration, where the
  passthrough resolver reads them.
- **Gate subclass scan** (AST): no framework module defines or imports a
  ``GateOutput``, ``GateInput``, or ``GateVerdict`` subclass, aliased bases
  included.
- **Framework DDL**: ``app/audit/sql`` creates no Postgres extension.
- **Root build metadata** (``pyproject.toml`` via ``tomllib``): the framework
  distribution depends on exactly the framework set (no reasoner, gate
  member, or gate infrastructure; ``httpx`` and ``starlette`` since Phase 3,
  for the HTTP client and the problem handlers), declares no optional
  extras and no entry points of its own, and ships only ``core``, ``app``,
  ``eval``; every third-party module the
  framework imports comes from that set. The uv workspace is exactly
  ``reasoners/*``, ``gates/*``, and ``integrations/*``.
- **Member build metadata**: each reasoner workspace member under
  ``reasoners/*`` declares its runtime plugin and eval adapter entry points
  itself, and each gate member under ``gates/*`` declares its gate entry
  point itself, pointing into its own import package. Gate members are
  reasoner-agnostic (no reasoner imports and no ATO tokens in their source,
  DDL, or index mappings), and a gate's DDL stays in its own schema.

Any new reasoner- or gate-specific symbol that leaks into framework code or
any new framework→plugin import site fails this test on the next
``make check``.
"""

from __future__ import annotations

import ast
import importlib.metadata as metadata
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
CORE_DIR = REPO_ROOT / "core"
EVAL_DIR = REPO_ROOT / "eval"
FRAMEWORK_SQL_DIR = APP_DIR / "audit" / "sql"
REASONERS_DIR = REPO_ROOT / "reasoners"
GATES_DIR = REPO_ROOT / "gates"
INTEGRATIONS_DIR = REPO_ROOT / "integrations"
CONFORMANCE_DIR = REPO_ROOT / "conformance"
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
    r"ato-reasoner|ato-v\w*|sparse_history|US_FEDERAL|US_STATE|EU_GDPR|"
    r"login[_ ]event|allow-band|block-band)\b"
)
ATO_WORD = re.compile(r"\bATO\b", re.IGNORECASE)

# Gate-member vocabulary that must not appear in framework code or config,
# matched case-insensitively by prefix: the rules gate's package, settings
# (RULES_GATE_*), types, and rule-set vocabulary; any gate distribution name;
# the policy gate's member package; the test-only fake gate.
GATE_MEMBER_TOKENS = re.compile(
    r"\b(rules_gate\w*|rulesets?\w*|decisionledger-gate-[\w-]+|"
    r"policy_rag_gate\w*|_fake_gate\w*|fake_?gate\w*)",
    re.IGNORECASE,
)

# Names of the infrastructure a gate owns (DR-27, DR-28): the search index,
# the vector extension, the embedding library, the LLM SDKs, the eval scorer,
# and the policy gate's chunk table. The framework's only infrastructure is
# Postgres, so none of these appear in framework code, DDL, or config —
# matched case-insensitively and inside identifiers (``openai_api_key``,
# ``ELASTICSEARCH_URL``), so prose mentions and settings fields count too.
GATE_INFRA_TOKENS = re.compile(
    r"(?<![A-Za-z0-9])(elasticsearch|pgvector|sentence[_-]transformers|openai|"
    r"anthropic|ragas|policy_chunks)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

#: The single exception to ``GATE_INFRA_TOKENS``, by enclosing identifier:
#: ``FaithfulnessMetrics.ragas_faithfulness`` is a key of the ``EvalReport``
#: schema in ``core/eval`` that predates the gate move and is stored in the
#: tracked eval baselines; renaming it is a report-schema change, not a move.
GATE_INFRA_IDENTIFIER_EXCEPTIONS: dict[str, frozenset[str]] = {
    "core/eval/metrics.py": frozenset({"ragas_faithfulness"}),
}

_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)

# The reference policy gate's internals. No framework module names any of
# them: the gate owns its retriever, prompts, LLM client, and retrieval
# record (DR-28), and since Phase 1b it lives in its own workspace member.
POLICY_GATE_TOKENS = re.compile(
    r"\b(PolicyGate\w*|GateResult|PolicyRetriever|RetrievedSnippet|"
    r"retrieval_query|retrieval_results|retrieval_path|index_version|"
    r"corpus_version|prompt_registry|PromptRegistry|YamlPromptRegistry|"
    r"Elasticsearch|SentenceTransformer|CrossEncoder|OpenAILLMClient|"
    r"AnthropicLLMClient|register_vector)\b"
)

#: Where the reference policy gate lived inside the framework trees until
#: Phase 1b moved it into ``gates/policy_rag/`` (directories end with ``/``).
#: None of them may hold a file again.
POLICY_GATE_FORMER_HOMES = (
    "app/gate/",
    "app/retrieval/",
    "app/llm/",
    "core/gate/policy/",
    "core/snippet.py",
    "eval/clients/",
    "eval/dimensions/citation.py",
    "eval/dimensions/faithfulness.py",
    "eval/dimensions/retrieval.py",
    "eval/judge.py",
    "eval/prompts/",
)

# The in-process handoff Phase 2 replaced with the wire ``Submission``
# (DR-31): the structural ``Observation`` protocol, its validator and error,
# the gate route and its three members, the fast-path rationale, the
# per-reasoner raw-event deserializer replay needed, the orchestrator that
# consumed them, the assembler entry point that built them, and the
# registration's gate allow-list (now its ``GateBinding`` gate set). Matched
# case-sensitively as whole identifiers, so the ``core.observation`` package
# (lower case) is untouched.
FORMER_HANDOFF_TOKENS = re.compile(
    r"\b(Observation|ObservationContractError|validate_observation|GateRoute|"
    r"FAST_PATH_ALLOW|FAST_PATH_BLOCK|ROUTE_TO_GATE|fast_path_rationale|"
    r"raw_event_factory|raw_event_factories|RawEventFactory|execute_decision|"
    r"build_observation|allowed_gate_ids)\b"
)

#: Committed training artifacts that record the band names of their time.
#: A model card is evidence of how a model was trained and evaluated, so it is
#: not rewritten; its band metrics keep the old keys. Each entry names the
#: exact tokens it may hold and must stay needed.
MEMBER_HANDOFF_TOKEN_EXCEPTIONS: dict[str, frozenset[str]] = {
    "reasoners/account_takeover/src/ato_reasoner/scorer/models/ato-v1.json": frozenset(
        {"FAST_PATH_ALLOW", "FAST_PATH_BLOCK", "ROUTE_TO_GATE"}
    ),
}

#: "Fast path" in any spelling, inside identifiers too (``fast_path_ms``,
#: ``FastPathRecord``, ``is_fast_path``), but not inside a longer word
#: ("breakfast path"). DR-30 repositions it: a reasoner records a decision it
#: made (citing a registered bypass rule) or asks for a gate.
FAST_PATH_PROSE = re.compile(r"(?<![a-z])fast[-_ ]?paths?", re.IGNORECASE)

#: Where the in-process handoff lived. None of them may hold a file again.
FORMER_HANDOFF_HOMES = (
    "core/routes.py",
    "core/observation/observation.py",
    "app/decide.py",
)

# How the framework hosted reasoners until Phase 3 (DR-31): it mounted each
# plugin's router into its app and built each plugin's request-time services
# onto ``app.state.reasoners``. A reasoner now runs as its own service, or in
# process through ``app.client.LocalClient``; the framework serves only its
# own routes.
FORMER_REASONER_HOSTING_TOKENS = re.compile(
    r"\b(ReasonerServices|build_services)\b|\bstate\.reasoners\b|"
    r"\bplugin\.router\b|\binclude_router\(\s*plugin"
)

#: Modules an integration member may import from ``app``: the clients and
#: the API's wire models. Anything else in ``app`` is the ledger's own runtime.
INTEGRATION_APP_MODULES = frozenset({"app.client", "app.api.models"})

#: Third-party roots the conformance suite may import: it drives an instance
#: over HTTP and reads its own example payloads.
CONFORMANCE_THIRD_PARTY = frozenset({"pytest", "httpx", "yaml"})

# Gate contracts whose subclasses the framework must never define or import:
# the bundle stores the universal base of each, and ``app/submit.py`` fails
# a gate closed when its output, verdict, or evidence reference is a
# subclass.
CLOSED_GATE_BASES = frozenset({"GateOutput", "GateInput", "GateVerdict"})

#: A household, in any case and inside identifiers too (``household_id``,
#: ``HouseholdView``). DR-32: the framework has no household concept. A
#: relationship type (household, segment, advisor book, ...) is something an
#: enterprise's identity resolver emits and declares when it is composed; the
#: framework stores, validates, and projects relationship types it is told.
HOUSEHOLD = re.compile(r"household", re.IGNORECASE)

#: The test-only identity resolver (``tests/_stub_resolver``), in any
#: spelling. Framework code knows resolvers only through the
#: ``IdentityResolver`` contract and the resolver entry-point group.
STUB_RESOLVER_TOKENS = re.compile(r"_?stub[-_ ]?resolver\w*", re.IGNORECASE)

#: Private subject-namespace constants members kept before DR-32. A reasoner
#: now declares each native namespace it names subjects in (and the UUID5
#: namespace the passthrough derives its ids under) in its registration, so
#: the ledger can assign the subject itself; the constant moved there.
FORMER_IDENTITY_TOKENS = re.compile(r"\b_ATO_ACCOUNT_NS\b")

#: The constructor of a UUID value (``UUID(...)``, ``uuid.UUID(...)``, or an
#: alias it is imported under). Bound to a module- or class-level name in a
#: reasoner member, such a value is a private subject namespace; inside a
#: ``NativeNamespace`` declaration it is an argument, not a binding.
UUID_CONSTRUCTORS = frozenset({"UUID"})

#: ``create extension`` in any form — the framework's DDL needs none.
CREATE_EXTENSION = re.compile(r"\bcreate\s+extension\b", re.IGNORECASE)

#: A schema-qualified reference to the framework's own schema.
FRAMEWORK_SCHEMA_REF = re.compile(r"\bdecisionledger\.\w", re.IGNORECASE)


def _py_files(root: Path) -> list[Path]:
    """Return every .py file under root, skipping caches."""
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _files(root: Path, *suffixes: str) -> list[Path]:
    """Every file under root with one of ``suffixes``, skipping caches."""
    return sorted(
        p
        for suffix in suffixes
        for p in root.rglob(f"*{suffix}")
        if "__pycache__" not in p.parts and p.is_file()
    )


def _scan_files() -> list[Path]:
    """Every .py, .yaml, .yml, .sql, and .json under the framework trees."""
    files: list[Path] = []
    for root in FRAMEWORK_DIRS:
        files.extend(_files(root, ".py", ".yaml", ".yml", ".sql", ".json"))
    return sorted(files)


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _gate_member_src_files() -> list[Path]:
    """Every .py, .yaml, .sql, and .json under each gate member's ``src/``."""
    files: list[Path] = []
    for src in sorted(GATES_DIR.glob("*/src")):
        files.extend(_files(src, ".py", ".yaml", ".yml", ".sql", ".json"))
    return sorted(files)


def _enclosing_identifier(text: str, start: int, end: int) -> str:
    """The identifier-like word around ``text[start:end]`` (letters, digits, _)."""
    while start > 0 and text[start - 1] in _IDENTIFIER_CHARS:
        start -= 1
    while end < len(text) and text[end] in _IDENTIFIER_CHARS:
        end += 1
    return text[start:end]


def _imported_roots(path: Path) -> set[str]:
    """Top-level names of every absolute import in ``path`` (AST, any depth)."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


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


def _workspace_member_packages() -> set[str]:
    """The import package of every reasoner, gate, and integration member."""
    packages: set[str] = set()
    for root in (REASONERS_DIR, GATES_DIR, INTEGRATIONS_DIR):
        for pyproject in _member_pyprojects(root):
            package = _member_package(
                tomllib.loads(pyproject.read_text(encoding="utf-8"))
            )
            if package is not None:
                packages.add(package)
    return packages


def test_framework_code_imports_no_workspace_member_by_any_name():
    """No framework module imports any reasoner or gate member (AST scan).

    The member packages are read from the workspace (each member's
    pyproject), so a reasoner or gate added later is covered without
    editing this test; aliases and function-local imports are caught too.
    """
    members = _workspace_member_packages()
    assert {"ato_reasoner", "rules_gate", "policy_rag_gate"} <= members, members
    forbidden = members | {"reasoner", "reasoners", "gates", "tests"}
    offenders = [
        (_rel(path), sorted(_imported_roots(path) & forbidden))
        for root in FRAMEWORK_DIRS
        for path in _py_files(root)
        if _imported_roots(path) & forbidden
    ]
    assert not offenders, f"framework code imports workspace members: {offenders}"


#: The only place an LLM SDK may be imported (DR-23): the RAG policy gate's
#: provider adapters.
_LLM_SDK_HOME = GATES_DIR / "policy_rag" / "src" / "policy_rag_gate" / "llm"
_LLM_SDKS = frozenset({"openai", "anthropic"})


def test_llm_sdks_are_imported_only_by_the_policy_gates_llm_adapters():
    """DR-23 across every tree: framework, reasoners, gates, integrations,
    the conformance suite, and tests.

    An AST scan, so indented and function-local imports count and docstring
    examples do not.
    """
    roots = (
        APP_DIR,
        CORE_DIR,
        EVAL_DIR,
        REASONERS_DIR,
        GATES_DIR,
        INTEGRATIONS_DIR,
        CONFORMANCE_DIR,
    )
    offenders = [
        (_rel(path), sorted(_imported_roots(path) & _LLM_SDKS))
        for root in (*roots, REPO_ROOT / "tests")
        for path in _py_files(root)
        if _LLM_SDK_HOME not in path.parents and _imported_roots(path) & _LLM_SDKS
    ]
    assert not offenders, f"LLM SDK imported outside {_rel(_LLM_SDK_HOME)}: {offenders}"


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


def test_no_gate_infrastructure_names_in_framework_code_or_config():
    """The framework names none of the infrastructure a gate owns.

    The search index, vector extension, embedding library, LLM SDKs, eval
    scorer, and chunk table all belong to the gate that uses them; the
    framework's only infrastructure is Postgres (DR-27). A mention here —
    an import, a setting, a DDL line, or a docstring — means gate machinery
    crept back into the framework.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        allowed = GATE_INFRA_IDENTIFIER_EXCEPTIONS.get(_rel(path), frozenset())
        hits = sorted(
            {
                _enclosing_identifier(text, m.start(), m.end())
                for m in GATE_INFRA_TOKENS.finditer(text)
            }
            - allowed
        )
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, f"framework code names gate-owned infrastructure: {offenders}"


def test_every_gate_infrastructure_exception_is_still_needed():
    """An exception that no longer matches anything is stale and must go."""
    stale = [
        f"{identifier} in {rel}"
        for rel, identifiers in GATE_INFRA_IDENTIFIER_EXCEPTIONS.items()
        for identifier in sorted(identifiers)
        if not re.search(
            rf"(?<![A-Za-z0-9_]){identifier}(?![A-Za-z0-9_])",
            (REPO_ROOT / rel).read_text(encoding="utf-8"),
        )
    ]
    assert not stale, f"stale gate-infrastructure exceptions: {stale}"


def test_framework_modules_do_not_name_policy_gate_internals():
    """No framework module names the reference policy gate's machinery.

    Retrieval records, the retriever, the prompt registry, and the LLM
    adapters are the policy gate's own (DR-28). Every framework module
    reaches a gate through ``GateRequest`` and ``GateOutput`` alone; there
    are no exemptions.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in POLICY_GATE_TOKENS.finditer(text)})
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, (
        "framework modules name the policy gate's internals (they belong to "
        f"the gate member): {offenders}"
    )


def test_the_policy_gate_left_the_framework_trees():
    """None of the policy gate's former framework homes holds a file.

    Phase 1b moved the gate's runtime, retrieval stack, LLM adapters, typed
    models, eval dimensions, and judge prompts into ``gates/policy_rag/``.
    A file reappearing at an old path is the gate creeping back.
    """
    leftovers: list[str] = []
    for home in POLICY_GATE_FORMER_HOMES:
        target = REPO_ROOT / home
        if home.endswith("/"):
            if target.is_dir():
                leftovers.extend(
                    _rel(p)
                    for p in target.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts
                )
        elif target.exists():
            leftovers.append(home)
    assert not leftovers, f"policy gate files left in the framework: {leftovers}"


# ---------------------------------------------------------------------------
# Submission contract — the in-process handoff is gone (DR-29..DR-31)
# ---------------------------------------------------------------------------


def _member_src_files() -> list[Path]:
    """Every .py, .yaml, .sql, and .json under each member's ``src/``."""
    files: list[Path] = []
    for src in sorted([*REASONERS_DIR.glob("*/src"), *GATES_DIR.glob("*/src")]):
        files.extend(_files(src, ".py", ".yaml", ".yml", ".sql", ".json"))
    return sorted(files)


def test_framework_names_no_part_of_the_former_observation_handoff():
    """No framework file names the handoff the wire ``Submission`` replaced.

    A reasoner hands the ledger a concrete ``Submission`` (DR-31): no
    structural ``Observation`` protocol, no gate route chosen by the
    reasoner, no free-text fast-path rationale, and no per-reasoner
    deserializer for replay, which needs no reasoner code.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in FORMER_HANDOFF_TOKENS.finditer(text)})
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, (
        f"framework files name the removed Observation handoff: {offenders}"
    )


def test_framework_never_says_fast_path():
    """DR-30: a decision is recorded or gated; the framework has no fast path."""
    offenders = [
        _rel(path)
        for path in _scan_files()
        if FAST_PATH_PROSE.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"framework files still say 'fast path': {offenders}"


def test_no_member_names_the_former_observation_handoff():
    """Reasoner and gate members build and read ``Submission``s only.

    A member that still names the protocol, the route, the rationale, or the
    deserializer is either dead code or a docstring describing a contract
    that no longer exists.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _member_src_files():
        text = path.read_text(encoding="utf-8")
        allowed = MEMBER_HANDOFF_TOKEN_EXCEPTIONS.get(_rel(path), frozenset())
        hits = sorted(
            {m.group(0) for m in FORMER_HANDOFF_TOKENS.finditer(text)} - allowed
        )
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, (
        f"member sources name the removed Observation handoff: {offenders}"
    )


def test_every_member_handoff_token_exception_is_still_needed():
    """An exception whose file or tokens are gone is stale and must go."""
    stale = [
        f"{token} in {rel}"
        for rel, tokens in MEMBER_HANDOFF_TOKEN_EXCEPTIONS.items()
        for token in sorted(tokens)
        if not (REPO_ROOT / rel).is_file()
        or not re.search(rf"\b{token}\b", (REPO_ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert not stale, f"stale member handoff-token exceptions: {stale}"


def test_the_observation_handoff_left_its_former_homes():
    """The gate route, the protocol module, and the old orchestrator are gone."""
    leftovers = [home for home in FORMER_HANDOFF_HOMES if (REPO_ROOT / home).exists()]
    assert not leftovers, f"former handoff modules still exist: {leftovers}"


# ---------------------------------------------------------------------------
# Ledger API — the framework serves its own routes only (DR-31, Phase 3)
# ---------------------------------------------------------------------------


def test_framework_no_longer_hosts_reasoners():
    """No framework file mounts a reasoner's router or builds its services.

    The ledger is a service reasoners call (``POST /api/v1/decisions``), or
    a function they call in process through ``LocalClient``; it never runs
    a reasoner's code. A reasoner plugin publishes its registration and the
    assets its gates read, nothing more.
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted(
            {m.group(0) for m in FORMER_REASONER_HOSTING_TOKENS.finditer(text)}
        )
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, f"framework files still host reasoners: {offenders}"


def _reasoner_member_names() -> set[str]:
    """Every workspace reasoner's distribution, package, and ``reasoner_id``."""
    names: set[str] = set()
    for pyproject in _member_pyprojects(REASONERS_DIR):
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        names.add(data["project"]["name"])
        package = _member_package(data)
        if package is not None:
            names.add(package)
        eps = data["project"].get("entry-points", {})
        names.update(eps.get("decisionledger.reasoners", {}))
    return names


def _name_pattern(name: str) -> str:
    """``name`` in any case, with ``-`` and ``_`` interchangeable."""
    return "".join("[-_]" if char in "-_" else re.escape(char) for char in name)


def test_framework_names_no_workspace_reasoner():
    """No framework file names any reasoner member, however it is spelled.

    The names are read from every member's pyproject (its distribution, its
    import package, and the ``reasoner_id`` its entry point is published
    under), so a reasoner added later is covered without editing this test.
    Matched case-insensitively, with ``-`` and ``_`` interchangeable, and
    inside longer identifiers too (``EXAMPLE_REASONER_ID``,
    ``ato_reasoner_url``): only a letter or digit right before the name
    breaks the match.
    """
    names = _reasoner_member_names()
    assert {"ato-reasoner", "ato_reasoner"} <= names, names
    alternatives = "|".join(sorted({_name_pattern(n) for n in names}))
    pattern = re.compile(rf"(?<![A-Za-z0-9])({alternatives})", re.IGNORECASE)
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        hits = sorted(
            {m.group(0) for m in pattern.finditer(path.read_text(encoding="utf-8"))}
        )
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, f"framework files name a workspace reasoner: {offenders}"


def test_conformance_suite_is_a_black_box():
    """The conformance suite reaches an instance over HTTP and nothing else.

    It is the artifact a reasoner team runs against any DecisionLedger
    instance instead of reading the code, so it imports no framework,
    member, or test module — only the standard library, ``pytest``,
    ``httpx``, ``yaml``, and its own helpers.
    """
    files = _py_files(CONFORMANCE_DIR)
    assert files, f"no conformance suite found under {_rel(CONFORMANCE_DIR)}"
    allowed = CONFORMANCE_THIRD_PARTY | {"conformance", "__future__"}
    offenders = [
        (_rel(path), sorted(roots))
        for path in files
        if (
            roots := {
                root
                for root in _imported_roots(path) - allowed
                if root not in sys.stdlib_module_names
            }
        )
    ]
    assert not offenders, f"conformance code imports beyond HTTP: {offenders}"


def _imported_modules(path: Path) -> set[str]:
    """Full dotted names of every absolute import in ``path`` (AST, any depth)."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_integration_members_reach_the_ledger_only_through_its_client():
    """An integration (the MCP server) is a ledger client, not a deployment.

    Its source imports no reasoner or gate member and nothing under
    ``tests``, and from ``app`` only the clients and the API's wire models:
    composing gates, writing bundles, or running enforcement is the
    ledger's job, reached over ``LocalClient`` or ``HttpClient``.
    """
    members = _member_pyprojects(INTEGRATIONS_DIR)
    assert members, "no integration workspace members found under integrations/*"
    forbidden_roots = (
        _workspace_member_packages()
        - {_member_package(tomllib.loads(p.read_text("utf-8"))) for p in members}
    ) | {"reasoner", "reasoners", "gates", "tests", "eval"}
    offenders: list[tuple[str, list[str]]] = []
    for src in sorted(INTEGRATIONS_DIR.glob("*/src")):
        for path in _py_files(src):
            modules = _imported_modules(path)
            bad = sorted(
                module
                for module in modules
                if module.split(".")[0] in forbidden_roots
                or (
                    module.split(".")[0] == "app"
                    and module not in INTEGRATION_APP_MODULES
                )
            )
            if bad:
                offenders.append((_rel(path), bad))
    assert not offenders, f"integration members bypass the ledger client: {offenders}"


def test_every_integration_member_publishes_no_plugin():
    """An integration declares no reasoner, gate, or eval-adapter entry point."""
    problems: list[str] = []
    for pyproject in _member_pyprojects(INTEGRATIONS_DIR):
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        groups = sorted(
            group
            for group in data["project"].get("entry-points", {})
            if group.startswith("decisionledger.")
        )
        if groups:
            problems.append(f"{_rel(pyproject)}: publishes {groups}")
    assert not problems, problems


# ---------------------------------------------------------------------------
# Identity contract — the framework assigns identity, it does not resolve it
# (DR-32, Phase 3b)
# ---------------------------------------------------------------------------


def test_framework_has_no_household_concept():
    """No framework file names a household, in any spelling (DR-32).

    The framework's identity contract is flat and typed: a resolver emits
    ``RelatedEntity(relationship_type, entity_type, entity_id)`` values of
    the relationship types it declares, and the framework validates,
    journals, and projects them without knowing what any of them means. A
    household is one enterprise's relationship type, never the framework's.
    """
    offenders = [
        _rel(path)
        for path in _scan_files()
        if HOUSEHOLD.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"framework files name a household: {offenders}"


def test_framework_names_no_test_resolver():
    """The stub resolver is a test double; framework code never names it."""
    offenders: list[tuple[str, list[str]]] = []
    for path in _scan_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in STUB_RESOLVER_TOKENS.finditer(text)})
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, f"framework code names the stub resolver: {offenders}"


def _module_level_uuid_constants(tree: ast.Module) -> list[str]:
    """Names a module binds, at module or class level, to a ``UUID(...)`` call.

    Function bodies are not scanned: parsing a UUID out of input there is
    ordinary code, not a constant.
    """
    constructors = set(UUID_CONSTRUCTORS) | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "uuid"
        for alias in node.names
        if alias.name in UUID_CONSTRUCTORS and alias.asname
    }
    names: list[str] = []
    scopes: list[list[ast.stmt]] = [tree.body]
    while scopes:
        for node in scopes.pop():
            if isinstance(node, ast.ClassDef):
                scopes.append(node.body)
                continue
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if not isinstance(value, ast.Call):
                continue
            func = value.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called in constructors:
                names.extend(t.id for t in targets if isinstance(t, ast.Name))
    return names


def test_no_member_keeps_a_private_subject_namespace():
    """A reasoner's subject namespace lives in its registration (DR-32).

    The passthrough resolver derives a reasoner's subject ids from the
    native namespaces its registration declares; a namespace constant kept
    privately in a member's event module would be a second source the
    ledger cannot see. Besides the former constants by name, no reasoner
    member binds a module- or class-level name to a ``UUID(...)`` call: the
    UUID5 namespace a reasoner derives its subject ids under is an argument
    of the ``NativeNamespace`` its registration declares, and its events
    derive ids through that declaration (``entity_id_of``).
    """
    offenders: list[tuple[str, list[str]]] = []
    for path in _member_src_files():
        text = path.read_text(encoding="utf-8")
        hits = sorted({m.group(0) for m in FORMER_IDENTITY_TOKENS.finditer(text)})
        if path.suffix == ".py" and path.is_relative_to(REASONERS_DIR):
            hits.extend(_module_level_uuid_constants(ast.parse(text)))
        if hits:
            offenders.append((_rel(path), hits))
    assert not offenders, f"members keep a private subject namespace: {offenders}"


def test_framework_ddl_creates_no_extension():
    """The framework's Postgres needs no extension (DR-27).

    The vector extension belongs to the gate that stores embeddings, and
    that gate's own DDL creates it.
    """
    sql_files = _files(FRAMEWORK_SQL_DIR, ".sql")
    assert sql_files, f"no framework DDL found under {_rel(FRAMEWORK_SQL_DIR)}"
    offenders = [
        _rel(p) for p in sql_files if CREATE_EXTENSION.search(p.read_text("utf-8"))
    ]
    assert not offenders, f"framework DDL creates an extension: {offenders}"


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


def test_framework_never_defines_or_imports_a_gate_contract_subclass():
    """DR-28: the closed discriminated union of DR-20 is gone.

    A ``GateOutput`` / ``GateInput`` / ``GateVerdict`` subclass would be a
    gate's private schema; the framework persists and replays the universal
    base plus the opaque ``evidence_snapshot`` / ``extra``, so it neither
    defines such a subclass nor imports one (``PolicyGateOutput``- or
    ``PolicyGateVerdict``-style names included).
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

#: The framework distribution's complete runtime dependency set: contracts,
#: API, Postgres, config, and logging (Phase 1b, plan section 2), plus, since
#: Phase 3, the HTTP client ``app.client.HttpClient`` speaks to a remote
#: ledger with and the ASGI toolkit the problem handlers import directly.
_FRAMEWORK_DEPENDENCIES = frozenset(
    {
        "fastapi",
        "httpx",
        "orjson",
        "psycopg",
        "pydantic",
        "pydantic-settings",
        "pyyaml",
        "starlette",
        "structlog",
        "uvicorn",
    }
)

#: A PEP 508 requirement's distribution name (before extras or specifiers).
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


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


def test_root_dependencies_are_exactly_the_framework_set():
    """The framework installs Postgres access and nothing a gate owns (DR-27).

    No search client, vector adapter, embedding stack, LLM SDK, or reasoner
    infrastructure: each lives with the member that uses it.
    """
    deps = _pyproject()["project"]["dependencies"]
    names = set()
    for requirement in deps:
        match = _REQUIREMENT_NAME.match(requirement)
        assert match, f"unparseable requirement {requirement!r}"
        names.add(match.group(1).lower().replace("_", "-"))
    assert names == _FRAMEWORK_DEPENDENCIES, (
        f"root project.dependencies: extra {sorted(names - _FRAMEWORK_DEPENDENCIES)}, "
        f"missing {sorted(_FRAMEWORK_DEPENDENCIES - names)}"
    )


def test_root_package_declares_no_optional_dependencies():
    """An extra would carry gate infrastructure in the framework's metadata."""
    project = _pyproject()["project"]
    assert not project.get("optional-dependencies"), (
        "root pyproject must declare no [project.optional-dependencies]: "
        f"{project.get('optional-dependencies')}"
    )


def _normalized(distribution: str) -> str:
    return re.sub(r"[-_.]+", "-", distribution).lower()


def test_framework_code_imports_only_declared_dependencies():
    """Every third-party import in core/app/eval comes from a declared dependency.

    The workspace venv installs every member, so an undeclared import (a
    package only a gate or reasoner pulls in) would pass every other check
    and fail only after a framework-only install.
    """
    first_party = {"core", "app", "eval", "__future__"}
    providers = metadata.packages_distributions()
    undeclared: list[tuple[str, str, list[str]]] = []
    for root in FRAMEWORK_DIRS:
        for path in _py_files(root):
            for name in sorted(_imported_roots(path) - first_party):
                if name in sys.stdlib_module_names:
                    continue
                dists = sorted({_normalized(d) for d in providers.get(name, [])})
                if not set(dists) & _FRAMEWORK_DEPENDENCIES:
                    undeclared.append((_rel(path), name, dists))
    assert not undeclared, (
        f"framework code imports packages outside [project] dependencies: {undeclared}"
    )


def test_root_package_declares_no_entry_points():
    """Reasoner and gate entry points belong to their own distributions.

    The interim policy-gate entry point of Phase 1a left with the gate
    (mirroring DR-26): the framework package publishes no plugin of its own.
    """
    project = _pyproject()["project"]
    assert not project.get("entry-points"), (
        "root pyproject must declare no [project.entry-points]: "
        f"{project.get('entry-points')}"
    )


def test_root_wheel_ships_only_framework_packages():
    """The framework wheel is exactly core, app, eval."""
    packages = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert set(packages) == {"core", "app", "eval"}, packages


def test_workspace_members_are_reasoners_gates_and_integrations():
    """Every plugin and client lives in a member directory of its kind."""
    members = _pyproject()["tool"]["uv"]["workspace"]["members"]
    assert sorted(members) == ["gates/*", "integrations/*", "reasoners/*"], members


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
    """A gate serves every reasoner: no reasoner imports, no ATO vocabulary.

    Covers the member's source, prompts, DDL, and index mappings.
    """
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


def test_gate_member_ddl_stays_out_of_the_framework_schema():
    """A gate owns its own Postgres schema (DR-28).

    Gate DDL never creates or alters anything in the framework's
    ``decisionledger`` schema; the framework's DDL is the only writer there.
    """
    offenders = [
        _rel(p)
        for p in _gate_member_src_files()
        if p.suffix == ".sql" and FRAMEWORK_SCHEMA_REF.search(p.read_text("utf-8"))
    ]
    assert not offenders, f"gate DDL touches the framework schema: {offenders}"
