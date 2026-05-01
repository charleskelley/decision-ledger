#!/usr/bin/env bash
# DecisionLedger session-mode indicator for Claude Code's status line.
#
# Reads the JSON state on stdin (workspace.current_dir, workspace.project_dir,
# etc.), classifies the cwd against the project tree, and emits one of:
#   [RESEARCHER] knowledge/<relpath>
#   [DEVELOPER] <relpath>
#   <basename>            (cwd is outside the project)
#
# Configured via .claude/settings.json statusLine entry. Kept fast because
# Claude Code re-runs this on every prompt (300ms debounce).

set -uo pipefail

# Resolve a fallback project root from this script's location:
#   <project>/.claude/scripts/mode-statusline.sh
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fallback_project="$(cd "$script_dir/../.." && pwd)"

DL_FALLBACK_PROJECT="$fallback_project" exec python3 -c '
import json, os, sys

try:
    state = json.loads(sys.stdin.read())
except Exception:
    state = {}

ws = state.get("workspace") or {}
cwd = ws.get("current_dir") or state.get("cwd") or os.getcwd()
project = os.environ.get("DL_FALLBACK_PROJECT", "") or ws.get("project_dir") or ""

knowledge = os.path.join(project, "knowledge") if project else ""

if knowledge and (cwd == knowledge or cwd.startswith(knowledge + os.sep)):
    rel = os.path.relpath(cwd, knowledge)
    suffix = "" if rel == "." else "/" + rel
    print(f"[RESEARCHER] knowledge{suffix}")
elif project and (cwd == project or cwd.startswith(project + os.sep)):
    rel = os.path.relpath(cwd, project)
    label = "(project root)" if rel == "." else rel
    print(f"[DEVELOPER] {label}")
else:
    print(os.path.basename(cwd) or "?")
'
