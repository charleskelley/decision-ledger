# DecisionLedger — justfile
# Run `just` with no args to list all tasks.
# Primary build automation is the Makefile. This justfile provides
# convenience recipes for tasks not covered there (e.g., diagrams).

default:
    @just --list

# ─── Environment ─────────────────────────────────────────────────────────────
setup:
    mise install
    uv sync --all-extras
    direnv allow

sync:
    uv sync

lock:
    uv lock

add pkg:
    uv add {{pkg}}

# ─── Quality ─────────────────────────────────────────────────────────────────
lint:
    uv run ruff check --fix .
    uv run ruff format .

format:
    uv run ruff format .

format-check:
    uv run ruff format --check .

typecheck:
    uv run pyright

test *args:
    uv run pytest tests/ {{args}}

check: lint format-check typecheck test

# ─── Training ───────────────────────────────────────────────────────────────
train *args:
    uv run python -m reasoner.account_takeover.scorer train {{args}}

# ─── Infrastructure ──────────────────────────────────────────────────────────
up:
    docker compose up -d

down:
    docker compose down

# ─── Utilities ───────────────────────────────────────────────────────────────
clean:
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -name "*.pyc" -delete 2>/dev/null || true
    rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build

# Render D2 architecture diagrams → SVG
# Requires: brew install d2
# Source:   docs/design/diagrams/*.d2
# Output:   docs/assets/diagrams/*.svg  (commit these)
# Layout:   components.d2 uses dagre (handles flat pipeline flow better);
#           all other diagrams use elk (handles nested containers better).
diagrams:
    #!/usr/bin/env sh
    set -e
    for src in docs/design/diagrams/*.d2; do
        name=$(basename "$src" .d2)
        if [ "$name" = "components" ]; then
            d2 --theme 0 "$src" "docs/assets/diagrams/${name}.svg"
        else
            d2 --theme 0 --layout elk "$src" "docs/assets/diagrams/${name}.svg"
        fi
    done

git:
    lazygit

pr:
    gh pr create --fill
