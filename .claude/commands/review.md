Review the current uncommitted changes (`git diff` and `git diff --staged`) against the project's standards:

1. **Style compliance:** Check against STYLE.md — Ruff formatting, Google docstrings, type hints, naming conventions.
2. **core/ boundary:** Verify no infrastructure imports leaked into `core/`.
3. **Pydantic conventions:** Verify `ConfigDict(strict=True, frozen=True)` on core models, v2 API only, nested models over dicts.
4. **Testing appropriateness:** For `core/` contracts and enforcement routing, verify behavioral tests exist for new logic. For self-explanatory utility functions, verify inline assertions are present instead of unnecessary test files. Do NOT flag missing tests for thin infrastructure wrappers or Pydantic field declarations.
5. **Error handling:** Check for bare `except:`, missing exception types, silent failures.
6. **Logging:** Verify structured logging with required fields (`component`, `event_id`, `decision_id`, `duration_ms`).

Report findings as a checklist with pass/fail for each category and specific file:line references for any issues.
