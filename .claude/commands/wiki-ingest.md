Ingest a source into the personal knowledge base under `knowledge/`. Argument: a path to a source file (typically under `knowledge/sources/`) or a URL.

**Before doing anything else, read `knowledge/CLAUDE.md`.** It defines the wiki's directionality (KB is upstream of DecisionLedger; code/docs never supersede KB knowledge), page conventions (frontmatter, `[[wikilinks]]`), and operations. Follow it strictly.

Ingest workflow:

1. **Read the source.** If a URL was given and the file does not yet exist under `knowledge/sources/`, ask the user to clip it via Obsidian Web Clipper first (target: `knowledge/sources/articles/`). Do not silently fetch from the web.
2. **Surface 3–5 key takeaways** for discussion. Pause for user input on framing/emphasis. Do not write or edit any wiki files yet.
3. **File the source page** under `knowledge/sources/<kind>/<slug>.md` with proper frontmatter (`type: source`, `title`, `created`, `clipped_from` or `cite`, `related`). If Web Clipper already created the file, edit its frontmatter rather than overwriting.
4. **Update or create wiki pages** under `knowledge/wiki/` that the source touches. A single source typically touches 5–15 pages. New concepts/strategies/topics get new pages. Cross-link aggressively with `[[basename]]`.
5. **Update `knowledge/index.md`** with new entries and revised one-line summaries on touched pages.
6. **Append `knowledge/log.md`** with `## [YYYY-MM-DD] ingest | <Source Title>` and a 2–3 sentence note on what changed.

Hard rules:
- Never claim a project file (`docs/design/*`, `core/*`, etc.) as authoritative for a wiki concept. Project files appear in wiki pages only as illustrations of KB ideas in action.
- Never silently overwrite a wiki claim that contradicts a new source. Update the page and note the change in `log.md`.
- Stay in dialog during ingest. Push back, ask the user what to emphasize, surface uncertainty.
