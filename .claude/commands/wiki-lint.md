Health-check the personal knowledge base under `knowledge/`. No argument required.

**Before doing anything else, read `knowledge/CLAUDE.md`.** It defines the wiki's structure and conventions. Follow it strictly.

Lint workflow:

1. **Read `knowledge/index.md`** to enumerate every wiki and source page.
2. **Walk every page** in `knowledge/wiki/` and `knowledge/sources/`. Build a mental map of frontmatter (title, type, created, sources, related) and inbound/outbound `[[wikilinks]]`.
3. **Report findings** as a structured checklist:
   - **Contradictions**: wiki pages whose claims conflict with each other or with sources.
   - **Stale claims**: pages whose listed sources have been superseded by newer ingests.
   - **Orphan pages**: pages with zero inbound `[[wikilinks]]`.
   - **Missing concept pages**: concepts referenced in body text or wikilinks but lacking a dedicated page.
   - **Missing cross-references**: pages that should link to each other but don't.
   - **Thin stubs**: pages that have been stubs for >30 days with no new content.
   - **Frontmatter drift**: pages with missing/malformed YAML frontmatter.
   - **Index drift**: `index.md` entries that don't match actual files (or vice versa).
   - **Knowledge gaps**: topics where a web search or new source would meaningfully strengthen the wiki. Suggest specific search terms.
   - **Directionality violations**: pages that treat `docs/design/*` or code as authoritative rather than illustrative.
4. **Propose specific edits** with file paths and exact text. Do NOT apply edits without the user confirming each one (or batch-confirming a group).
5. **Append `knowledge/log.md`**: `## [YYYY-MM-DD] lint | <one-line summary>` describing what was found and what (if anything) was applied.

Hard rules:
- The lint pass is read-mostly. The only file that gets written without confirmation is `log.md` at the end.
- Surface knowledge gaps as questions the user might want to investigate, not as definitive holes. The user decides what to chase.
