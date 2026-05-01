#!/usr/bin/env bash
# .claude/hooks/block-git-commit.sh
# Blocks Claude from running git commit directly.
# The user must always be the committer.
set -euo pipefail

# Extract the command from Claude's tool input
cmd=$(echo "$CLAUDE_TOOL_INPUT" | jq -r '.command // ""' 2>/dev/null || echo "")

# Block any git commit variant
if echo "$cmd" | grep -qE '^\s*git\s+commit'; then
    echo "BLOCKED: Claude must not run git commit directly. Stage files with 'git add' and suggest a commit message, then let the user commit." >&2
    exit 2
fi

# Block git push --force variants
if echo "$cmd" | grep -qE '^\s*git\s+push\s+(-f|--force)'; then
    echo "BLOCKED: Force push is not allowed." >&2
    exit 2
fi

# Block git reset --hard
if echo "$cmd" | grep -qE '^\s*git\s+reset\s+--hard'; then
    echo "BLOCKED: Hard reset is not allowed." >&2
    exit 2
fi

exit 0
