Run the full quality check suite in order. Stop and report if any step fails:

1. `uv run ruff format --check .` — verify formatting
2. `uv run ruff check .` — verify linting
3. `uv run python -m pytest tests/core/ -x -q` — run core unit tests (fast)
4. Report a summary of what passed and what failed.

Do NOT run integration tests or the eval harness — those are slow and should be run explicitly. If the user wants a quick end-to-end sanity check, suggest `make test-smoke` (requires Docker).
