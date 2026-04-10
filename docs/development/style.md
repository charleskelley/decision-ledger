# Style Guide

> DecisionLedger follows established, well-documented conventions rather than
> inventing its own. This document lists the specific guides we follow,
> project-specific conventions, and any local overrides.

---

## Python

### Formatting & Linting

| Concern         | Guide                                                                                                         | Enforcement                                    |
| --------------- | ------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| Code style      | [PEP 8](https://peps.python.org/pep-0008/)                                                                    | Ruff (auto-format + lint)                      |
| Import ordering | isort conventions via Ruff                                                                                    | Ruff `I` rules                                 |
| Type hints      | [PEP 484](https://peps.python.org/pep-0484/), [PEP 604](https://peps.python.org/pep-0604/) (`X \| None`)      | Ruff + pyright in CI                           |
| Docstrings      | [Google Python Style Guide §3.8](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings) | Ruff `D` rules (pydocstyle, google convention) |
| General style   | [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)                                 | Code review                                    |
| Line length     | 88 characters                                                                                                 | Ruff `line-length = 88`                        |

### Ruff Configuration

Ruff is the single tool for formatting and linting. No black, no isort, no
flake8, no pylint — Ruff replaces all of them.

```toml
# pyproject.toml
[tool.ruff]
target-version = "py311"
line-length = 88

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort
    "N",    # pep8-naming
    "D",    # pydocstyle
    "UP",   # pyupgrade
    "B",    # flake8-bugbear
    "SIM",  # flake8-simplify
    "TCH",  # flake8-type-checking
    "RUF",  # ruff-specific rules
]
ignore = [
    "D100",  # missing docstring in public module (too noisy early on)
    "D104",  # missing docstring in public package
]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.isort]
known-first-party = ["core", "app", "evaluation", "scenarios"]
```

### Pydantic Conventions

Pydantic v2 is the schema validation layer across the entire pipeline. Follow
these patterns consistently:

- **Strict mode by default** for all models in `core/`. Use `model_config =
ConfigDict(strict=True)` unless there is a documented reason to relax.
- **`field_validator`** for single-field validation; **`model_validator`** for
  cross-field validation. Do not use `@validator` (v1 API).
- **Immutable models** in `core/`: use `model_config = ConfigDict(frozen=True)`
  for all contract types (event schemas, decision bundles, enforcement outputs).
  Mutable models are acceptable in `app/` for state that changes during
  processing.
- **Explicit serialization aliases** when JSON field names differ from Python
  attribute names. Prefer `alias_generator` over per-field aliases when the
  pattern is systematic (e.g., camelCase API output).
- **Nested models over nested dicts.** If a field contains structured data,
  define a Pydantic model for it. `Dict[str, Any]` is a last resort.
- **Enum types for constrained values.** `Action`, `Jurisdiction`, `RiskTier`,
  `DocumentType` — define these as `StrEnum` in `core/` and reference them
  everywhere.

### Type Hints

- All public functions and methods must have complete type annotations
  (parameters and return type).
- Use Python 3.11+ syntax: `list[str]` not `List[str]`, `str | None` not
  `Optional[str]`.
- Private/internal helper functions should have type annotations but this is
  not enforced in CI during MVP.
- Use `TypeAlias` for complex repeated types.

### Naming

Follow PEP 8 naming plus these project-specific conventions:

- **Modules:** `snake_case.py` — always.
- **Pydantic models:** `PascalCase` — `LoginEvent`, `DecisionBundle`, `PolicyGateOutput`.
- **Enum members:** `UPPER_SNAKE_CASE` — `Action.ALLOW`, `Action.BLOCK`.
- **Feature names:** `snake_case` strings — `velocity_1min`, `novelty_device`, `geo_impossible_flag`. These appear in feature vectors and SHAP output; consistency matters for downstream readability.
- **Configuration keys:** `snake_case` in Python, `UPPER_SNAKE_CASE` for environment variables.
- **Test files:** `test_<module>.py` — mirrors the module being tested.

---

## Docstrings

Follow the Google Python Style Guide docstring convention. The key patterns:

**Module-level:** One-liner describing the module's purpose. Required for all modules in `core/`.

**Class-level:** Describe what the class represents, not how it works internally. For Pydantic models, the docstring describes the contract.

**Function/method-level:** Describe what the function does, its args, return value, and any exceptions raised. Use Google-style sections:

```python
def compute_risk_score(features: FeatureVector, model: ScorerModel) -> ScorerOutput:
    """Compute risk score from engineered features using the fast scorer.

    Produces a risk score in [0.0, 1.0] with the top-k contributing features
    and their SHAP values. Used for high-confidence triage before the LLM
    policy gate.

    Args:
        features: Computed feature vector from the online feature service.
        model: Loaded XGBoost model with version metadata.

    Returns:
        ScorerOutput containing risk_score, top_k_features, scorer_version,
        and inference_latency_ms.

    Raises:
        ScorerInferenceError: If model inference fails or produces invalid output.
    """
```

**When to skip:** Obvious one-liner private methods (`def _hash_key(self) -> str`) don't need a docstring if the name and type signature are self-documenting. Use judgment.

---

## Project Structure & Dependencies

### The `core/` Boundary

This is the most important structural convention in the project:

- `core/` imports from **nothing internal** and has **no infrastructure dependencies**. No Redis, no database drivers, no LLM SDK, no FastAPI, no HTTP clients.
- `core/` contains only: Pydantic schemas, enum definitions, contract interfaces (abstract base classes or protocols), pure business logic, and metric computation functions.
- Everything in `app/`, `eval/`, and `generator/` **imports from `core/`**. Nothing imports from `app/` except `app/` itself.
- Violation of this boundary is a blocking review finding. If you need to add an import to `core/`, stop and reconsider.

### File Organization

- One primary class or concern per module. A module can contain closely related helpers, but if it exceeds ~300 lines, consider splitting.
- `__init__.py` files export the public API of each package. Internal modules are prefixed with `_` if they should not be imported directly.
- Test files mirror the source tree: `core/decision/bundle.py` → `tests/core/decision/test_bundle.py`.

---

## API Design (FastAPI)

Follow the [Google API Improvement Proposals (AIP)](https://google.aip.dev/) for naming and structure conventions where applicable. Key patterns:

- **Resource-oriented endpoints:** `/api/v1/decisions`, `/api/v1/decisions/{id}`, `/api/v1/decisions/{id}/replay`.
- **Consistent error responses:** All errors return a JSON body with `error_code`, `message`, and `details`. Use FastAPI exception handlers to enforce this.
- **Versioned API prefix:** `/api/v1/` — even if there's only one version during MVP.
- **HTTP status codes:** `200` for successful decisions, `201` for created resources, `400` for validation errors, `422` for schema failures, `500` for internal errors. The decision action (ALLOW/BLOCK/etc.) is in the response body, not the status code.

---

## Logging & Observability

Follow [OpenTelemetry Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/) for field naming in structured logs.

- **Structured JSON logging** via Python's `logging` module with a JSON formatter. No print statements in production code.
- **Every log entry includes:** `timestamp`, `level`, `component` (e.g., `ingestion`, `scorer`, `policy_gate`, `enforcement`), `event_id` (when available), `decision_id` (when available).
- **Latency fields:** `duration_ms` (not `latency`, not `time_elapsed`, not `elapsed_ms` — pick one and use it everywhere).
- **Log levels:** `DEBUG` for detailed processing steps, `INFO` for decision outcomes and pipeline events, `WARNING` for degraded behavior (e.g., reranking bypass), `ERROR` for failures that affect output, `CRITICAL` for system-level failures.

---

## Infrastructure as Code (Terraform)

Follow [HashiCorp Terraform Style Conventions](https://developer.hashicorp.com/terraform/language/style):

- **File naming:** `main.tf`, `variables.tf`, `outputs.tf`, `providers.tf`, `versions.tf` per module.
- **Resource naming:** `snake_case` for resource names, descriptive labels: `aws_ecs_service.policy_gate`, not `aws_ecs_service.svc1`.
- **Variables:** Always include `description` and `type`. Use `validation` blocks for constraints. Provide `default` only when a sensible default exists.
- **Module structure:** `infra/modules/<component>/` for reusable modules, `infra/environments/<env>/` for environment-specific configs.
- **No hardcoded values:** Region, account ID, instance sizes — all variables.

---

## Database

### Schema Naming

Follow the [dbt style guide](https://docs.getdbt.com/best-practices/how-we-style/2-how-we-style-our-sql)
for general SQL conventions. The modifications below apply where operational
schema requirements differ from dbt's analytics-oriented defaults.

**SQL keywords — lowercase**

Write all SQL keywords in lowercase. This applies to DDL and DML alike:
`create table`, `select`, `insert into`, `references`, `not null`.

**Tables — plural `snake_case`**

| Correct | Incorrect |
|---|---|
| `decision_bundles` | `decision_bundle` |
| `policy_chunks` | `policy_chunk` |
| `replay_logs` | `replay_log` |

**Columns — singular `snake_case`**

Scalar columns represent a single normalized attribute and are singular.
Foreign key columns use the same name as the primary key they reference:

| Correct | Incorrect |
|---|---|
| `decision_id` (FK → `decision_bundles.decision_id`) | `decisions_id` |
| `entity_id` | `entities_id` |
| `final_action` | `final_actions` |

**Array columns — plural `snake_case`**

Array-typed columns are plural. They are inline collections and follow the
same convention as table names:

| Correct | Incorrect |
|---|---|
| `override_logs text[]` | `override_log text[]` |
| `permitted_actions text[]` | `permitted_action text[]` |

**Timestamps**

Use `timestamptz` for all timestamp columns. Never use
`timestamp without time zone`. UTC is enforced at the type level — do not add
`_utc` suffixes to column names. Use `_at` for event timestamps
(`created_at`, `replayed_at`) and `_timestamp` for pipeline processing times
(`ingestion_timestamp`).

**Identifiers**

Primary keys use `uuid` type and the `_id` suffix. Do not use `serial` or
`bigserial`. Foreign key column names match the referenced primary key exactly.

**Schema qualification**

Fully qualify all table references in SQL files:
`account_takeover.decision_bundles`, not `decision_bundles`.

---

## Git & Version Control

### Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>

<optional body>
```

**Types:** `feat`, `fix`, `docs`, `test`, `refactor`, `build`, `ci`, `chore`.

**Scopes** (project-specific): `core`, `ingestion`, `features`, `scorer`, `retrieval`, `policy-gate`, `enforcement`, `audit`, `eval`, `scenarios`, `infra`, `ci`.

**Examples:**

```
feat(scenarios): add credential_stuffing_burst generator with RBA-calibrated velocity
fix(enforcement): route schema validation failures to HOLD
docs(decisions): add D10 — hybrid data strategy and heuristic labeling
test(eval): add consistency tests for 8 scenarios × 3 orderings
refactor(core): extract Action enum to shared types module
```

### Branch Naming

`<type>/<short-description>` — e.g., `feat/scenario-generator`, `fix/replay-determinism`, `docs/decisions-d10`.

### PR Discipline

- PRs should be reviewable in one sitting. If a PR touches more than ~400 lines of non-test code, consider splitting.
- Every PR that changes pipeline behavior must include or update tests.
- Every PR that changes a prompt version must include a passing eval run.

---

## Testing Philosophy

This project follows behavior-driven testing principles. Tests exist to verify that the system **behaves correctly** — not to achieve a coverage number or to restate what the code already says. A test earns its place in the repo by catching a failure you actually care about. If a test can't fail in a way that matters, it's maintenance overhead.

### Where Tests Concentrate

Not all code needs the same testing rigor. Testing effort concentrates where failure consequences are highest:

**`core/` contracts — thorough behavioral testing.** The Pydantic models, enforcement rules, action enums, and evaluation metric interfaces are the contracts every other module depends on. If a `DecisionBundle` accepts invalid data or the enforcement router misclassifies a trigger, the downstream pipeline is wrong. Test these exhaustively — but test _behavior_, not field defaults. "Given this combination of inputs, what action does the enforcement layer produce?" is a valuable test. "Does this field default to None?" is not.

**Enforcement routing logic — exhaustive case coverage.** The enforcement layer is the governance guarantee. Five trigger types, priority ordering, review packet construction, override logging. Every trigger must have explicit test cases. Every priority interaction must be tested. Every edge case where two triggers fire simultaneously must be tested. This is the one area where near-complete case coverage is justified, because a bug here breaks the core claim of the project.

**Integration tests across the decision path — highest value per test.** An event goes in, a Decision Bundle comes out. Does the right scenario produce the right action? Does replay produce identical output? These tests prove the system works end-to-end. They're slower (Docker required), but they test the thing that matters. Mark with `@pytest.mark.integration`.

**Smoke tests — fast confidence during development.** A minimal subset of integration tests: one happy path, one error path, one replay. Tag 3–5 tests with `@pytest.mark.smoke`. When you want faster feedback than the full integration suite but more confidence than unit tests alone, run `make test-smoke`. These require Docker but finish quickly.

**The eval harness IS the test suite for LLM-touching code.** The 5-dimension evaluation framework with CI gates is more rigorous than any unit test for the policy gate. Do not unit-test LLM output with mocked responses — that creates brittle tests that prove nothing about real behavior. The eval harness, running against actual or recorded LLM responses, is the right testing strategy for everything downstream of the policy gate. Mark with `@pytest.mark.evaluation`. Note: evaluation tests make LLM API calls and cost money — don't run them in a tight loop during development.

**Scenario generator — tested via its outputs.** The scenario generator is validated by its consumers: do the generated events conform to the schema? Do all 8 scenarios produce events with the correct statistical properties? Do the features exercise the right parts of the pipeline? Test the outputs, not the internal generation logic.

### Where Tests Are Not Needed

**Self-explanatory pure functions.** If a utility function is 5 lines, the type signature makes the contract clear, and an inline `assert` in the function body validates the invariant on every call — a separate test file adds overhead without adding confidence. The assertion runs in dev and test on every invocation; a test file runs only when you remember to invoke it.

```python
# This doesn't need a separate test file:
def compute_idempotency_key(user_id: str, device_id: str, event_type: str, ts_bucket: int) -> str:
    """Compute deterministic idempotency key from event identity fields."""
    raw = f"{user_id}:{device_id}:{event_type}:{ts_bucket}"
    result = hashlib.sha256(raw.encode()).hexdigest()
    assert len(result) == 64  # SHA-256 invariant
    return result
```

**Pydantic model field declarations.** Pydantic's own validation is thoroughly tested by the Pydantic project. Testing that `field: str` rejects an `int` is testing Pydantic, not your code. Test your custom validators, your `model_validator` cross-field logic, and your `ConfigDict` constraints — not field type declarations.

**Thin wrappers around infrastructure clients.** If `app/retrieval/dense.py` calls `pgvector` and returns results, the interesting test is the integration test that checks retrieval quality — not a unit test with a mocked database that proves you called the mock correctly.

### Inline Assertions vs. Separate Tests

Use inline assertions for invariants that should hold on every execution:

```python
def resolve_action(gate_output: PolicyGateOutput, triggers: list[RoutingTrigger]) -> EnforcementResult:
    """Apply deterministic enforcement rules to policy gate output."""
    assert gate_output.permitted_actions, "Policy gate must return at least one permitted action"
    # ... enforcement logic ...
    assert result.action in gate_output.permitted_actions or result.override_applied
    return result
```

These assertions document the contract, catch violations during development, and run on every call path — not just the paths your test suite happens to exercise. They complement tests; they don't replace them. For the enforcement layer, you want _both_: inline assertions for invariants plus exhaustive test cases for routing behavior.

### Test Organization

Tests mirror the source tree:

```
tests/
├── core/
│   ├── decision/
│   │   ├── test_bundle.py          # Bundle construction, validation, serialization
│   │   └── test_action_space.py    # Action enum behavior
│   └── policy/
│       └── test_enforcement.py     # Exhaustive routing trigger tests
├── app/
│   ├── ingestion/
│   │   └── test_dedup.py           # Idempotency key computation, bounded lateness
│   ├── scorer/
│   │   └── test_risk_score.py      # Score range validation, feature contribution
│   └── retrieval/
│       └── test_hybrid_search.py   # Integration: retrieval quality on golden queries
└── integration/
    ├── test_decision_path.py       # End-to-end: event in → bundle out
    └── test_replay.py              # Replay produces identical enforcement output
```

### Test Naming

Test names describe the behavior being verified, not the function being called:

```python
# Good — describes the behavior
def test_enforcement_routes_schema_failure_to_HOLD(): ...
def test_enforcement_applies_novel_entity_trigger_when_history_below_threshold(): ...
def test_replay_produces_identical_action_from_logged_bundle(): ...

# Bad — describes the function call
def test_resolve_action(): ...
def test_enforcement_1(): ...
def test_replay(): ...
```

### What "Passing Tests" Means for a PR

- All `core/` tests pass (always — these are fast and non-negotiable).
- Smoke tests pass if the PR touches any pipeline component (quick sanity check).
- Integration tests pass if the PR touches pipeline behavior.
- Eval harness passes if the PR touches the policy gate, retrieval, or prompt versions.
- New enforcement routing logic includes tests for every trigger path.
- New `core/` contracts include behavioral tests for custom validation logic.

---

## Decision Records

DECISIONS.md entries use the [Nygard ADR format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions) (Context → Decision → Status → Consequences) but are numbered as **Decision Records (DR-N)** rather than Architecture Decision Records (ADR-N). This is deliberate: the decisions that matter in this project span architecture, design, implementation strategy, and tooling. They all belong in one document because they all affect how the system works and why. Splitting them by category or qualifying each one's "level" adds process overhead with no value. Anyone familiar with ADRs will recognize the format and the discipline immediately — the prefix doesn't change that signal.

Each entry follows four sections:

1. **Context** — What problem were you solving?
2. **Decision** — What did you choose?
3. **Status** — Accepted, Proposed, Deprecated, or Superseded by DR-N.
4. **Consequences** — What does this decision make easier _and harder_? What alternatives were rejected and why?

The **Consequences** framing is important: it forces articulation of tradeoffs, not just justification. A decision record that only explains why a choice was right is advocacy; one that also explains what was given up is engineering.

Each entry should be written as it's built, not retroactively. The goal is interview-ready material: "walk me through an interesting design decision."

---

## Documentation

### README

- Opens with the problem statement, not the solution.
- Architecture diagram (Mermaid) appears above the fold.
- Quickstart (`docker compose up && make scenario`) within the first screenful.
- No badges wall. One or two meaningful badges (CI status, Python version) maximum.

### Inline Documentation

- Prefer self-documenting code (clear names, small functions, typed signatures) over comments explaining _what_.
- Use comments to explain _why_ — especially for non-obvious design choices, performance tradeoffs, or workarounds.
- `# TODO:` is acceptable during MVP. Each TODO must include a brief description of what's needed. No orphan TODOs — they must reference a phase or sprint.

### Diagrams

- **README:** Mermaid (GitHub renders natively).
- **docs/ site:** PlantUML with C4-PlantUML library.
- **Both committed as text files** — no binary exports, no PNGs checked in.
