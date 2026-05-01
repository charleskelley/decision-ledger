Display the following knowledge base user manual to the user. Render the headings, bullets, tables, and code blocks verbatim. Do not paraphrase the posture summaries, directionality rule, or handoff format — those phrasings are referenced from other commands and CLAUDE.md files and must stay consistent. If the user asks a follow-up question after the manual, elaborate freely; the manual itself is the canonical reference.

---

# Knowledge Base User Manual

How to use the personal knowledge base under `knowledge/` from two perspectives: the **developer** working on DecisionLedger code and docs, and the **researcher** working on the wiki itself. Both share one wiki — the difference is posture and default workflow.

## How the Two Modes Work

Claude Code auto-loads `CLAUDE.md` from the current working directory upward. The persona is set by where the session starts; there is no toggle.

- **Developer mode** — open Claude Code from the project root (`decision-ledger/`). Only the root `CLAUDE.md` auto-loads. The KB schema is *not* in scope unless explicitly read.
- **Researcher mode** — open Claude Code from `decision-ledger/knowledge/`. Both `knowledge/CLAUDE.md` (wiki schema, primary) and the root `CLAUDE.md` (project rules, ambient) auto-load.

The status line shows the active mode persistently: `[DEVELOPER] <relpath>` or `[RESEARCHER] knowledge/<relpath>` (or a bare basename when cwd is outside the project). Run `/wiki-mode` to confirm verbally — useful before risky operations.

## Developer Mode (project root)

You are a software engineer working on DecisionLedger. The wiki is reference material — read when relevant, never write to it.

**Posture:**
- Codebase and `docs/design/*` are your primary surface.
- The KB is **upstream knowledge** — when a design question touches a tracked concept (audit replay, hybrid RAG, LLM-as-judge, deterministic enforcement, etc.), read `knowledge/index.md` first to find the relevant page, then drill in.
- Pull KB knowledge into reasoning ("this is informed by `[[audit-replay-pattern]]`"). Never push code facts into the KB.
- `knowledge/` is **read-only** in this mode. Do not modify wiki files even when implementing findings from a synthesis. Checkbox status updates come from the user or a follow-up researcher-mode session.

**Anti-patterns:**
- Updating a wiki page because the code does X. Wiki updates need a *better external source*, not code changes.
- Treating `docs/design/*` as canonical for a KB concept. Code and docs are illustrations of KB ideas, never their definitions.

## Researcher Mode (`knowledge/` dir)

You are a curator and researcher working on the wiki itself.

**Posture:**
- Wiki maintenance is your primary task.
- Stay in dialog during ingest. Surface 3–5 takeaways before writing anything; pause for framing/emphasis.
- For substantive query answers, offer to file under `wiki/syntheses/`.
- Lint periodically (every ~10 ingests, or when the graph view looks lopsided).

**Propose, don't execute** for codebase or `docs/` changes. When research surfaces actionable DecisionLedger work, file a synthesis with a handoff section (see below). Do not edit the project from researcher mode.

## Cross-Mode Handoff (Synthesis Transfer)

The intended workflow is **two open sessions** — one developer, one researcher — with handoffs via synthesis pages, not by switching modes inside a single session.

**Synthesis page format** for handing codebase work from researcher to developer:

```markdown
## Next steps for DecisionLedger

> **Hand-off.** Open a developer-mode session from the project root and reference this file (`knowledge/wiki/syntheses/<slug>.md`).

- [ ] **<Concrete change>** — `<file path>` (anchor: <symbol or section>). Rationale: [[concept]], [[source-page]].
- [ ] **<Concrete change>** — ...
```

**Developer-mode behavior on receiving a synthesis path:**
1. Read the synthesis page in full.
2. Walk the `## Next steps for DecisionLedger` checkboxes in order.
3. For each, propose the implementation with diffs and ask before applying.
4. Do **not** modify the synthesis page — including the checkboxes. The user (or a follow-up researcher-mode session) marks them done after verifying.

**Researcher-mode behavior when developer-mode would be more appropriate:**
- If a question requires deep code exploration or runtime testing, surface "this is better suited to a developer-mode session" rather than guessing inline.

## Directionality (holds in both modes)

The KB is **upstream** of DecisionLedger. Citations flow KB → project. Code and `docs/design/*` never supersede KB knowledge. Links from KB pages to `docs/design/*` appear only as supporting examples of KB ideas in action — never as canonical definitions of those ideas. This rule does not change between modes; only the workflow does.

## Slash Commands & Status Line

| Trigger | Effect |
|---|---|
| `/wiki-man` | Show this manual. |
| `/wiki-mode` | Report current mode + posture summary. Reporter only — does not switch modes. |
| `/wiki-ingest <path-or-url>` | Ingest a source into the wiki. Pauses for dialog. Researcher mode is the typical caller. |
| `/wiki-lint` | Health-check the wiki. Read-only until you confirm proposed edits. |

Status line script: `.claude/scripts/mode-statusline.sh`. Configured in `.claude/settings.json`. Restart Claude Code to pick up status-line changes.

## First-Run Tips

- Open `knowledge/` in Obsidian (separate window) to see the wiki visually as it updates.
- Configure Obsidian Web Clipper to drop articles into `knowledge/sources/articles/`.
- Set Obsidian's attachment folder path to `sources/assets/` and bind a hotkey for "Download attachments for current file."
- After your first few ingests, run `/wiki-lint` to catch convention drift early.

## See Also

- Root `CLAUDE.md` — project rules and developer-mode KB instructions.
- `knowledge/CLAUDE.md` — wiki schema, page conventions, ingest/query/lint workflows in detail.
