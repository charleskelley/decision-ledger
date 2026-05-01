Report which mode this Claude session is operating in. Used to confirm session posture in conversation (the persistent indicator is the status line; this command is the verbal confirmation).

1. Run `pwd` to read the current working directory.
2. Classify against the DecisionLedger project tree (`/Users/Charles/Documents/Projects/decision-ledger`):
   - cwd is the `knowledge/` subdirectory or any descendant → **RESEARCHER mode**
   - cwd is under the project root but NOT under `knowledge/` → **DEVELOPER mode**
   - otherwise → outside DecisionLedger context, neither mode active
3. Report:
   - The mode label (or "neither")
   - cwd shown as a path relative to the project root
   - The one-sentence posture summary for the active mode (see below)
   - The KB slash commands available

**Posture summaries** (use verbatim, don't paraphrase — the user relies on consistent wording):

- **RESEARCHER**: wiki maintenance is primary. Stay in dialog during ingest. For substantive query answers, offer to file under `wiki/syntheses/`. **Propose, don't execute** for code/docs changes — hand off to developer mode via a synthesis page with a `## Next steps for DecisionLedger` section.

- **DEVELOPER**: codebase and `docs/design/*` are primary. The KB is reference material — read `knowledge/index.md` first when a question touches a tracked concept. Pull KB knowledge into reasoning; never push code facts into the KB. Treat `knowledge/` as **read-only** — do not modify wiki files even when implementing findings from a researcher-mode synthesis.

**KB slash commands** (active in both modes when the KB is present): `/wiki-man`, `/wiki-mode`, `/wiki-ingest`, `/wiki-lint`.

Hard rules:
- This command is a reporter. It must not modify any files.
- If cwd doesn't match either mode, report "neither" — do not assume one based on session history.
