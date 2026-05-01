#!/usr/bin/env bash
# .claude/hooks/post-edit-lint.sh
# Runs Ruff format + check on Python files after Claude edits them.
# Non-blocking: reports issues but doesn't prevent the edit.
set -euo pipefail

# Extract the file path from Claude's tool output
file_path=$(echo "$CLAUDE_TOOL_INPUT" | jq -r '.file_path // .filePath // ""' 2>/dev/null || echo "")

# Only lint Python files
if [[ "$file_path" != *.py ]]; then
    exit 0
fi

# Check if ruff is available
if ! command -v ruff &>/dev/null; then
    exit 0
fi

# Run ruff format (auto-fix)
ruff format "$file_path" 2>/dev/null || true

# Run ruff check with auto-fix
ruff check --fix "$file_path" 2>/dev/null || true

# Report any remaining issues (non-blocking)
remaining=$(ruff check "$file_path" 2>/dev/null || true)
if [[ -n "$remaining" ]]; then
    echo "Ruff issues remaining in $file_path:" >&2
    echo "$remaining" >&2
fi

exit 0
