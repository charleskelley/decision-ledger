# Documentation Tooling: MkDocs → Zensical Migration

!!! info "Status 2026-10-18 — migration planned, not yet executed"
    The site still builds and deploys with MkDocs + Material for MkDocs.
    Zensical is the chosen successor. A trial build on 2026-10-18 produced the
    same site with zero warnings, and CI now runs an advisory Zensical build
    beside the MkDocs one. The switch happens once Zensical 0.1.0 has held
    for a few months. Read this if you are evaluating docs tooling or need to
    know what the migration involves.

---

## Background

MkDocs stopped moving in 2024 (last release 1.6.1, August 2024; last commit
October 2025). The ecosystem around it then split:

| Project | Status (October 2026) |
|---------|-----------------------|
| **Material for MkDocs** | Maintenance mode since 9.7.0 (2025-11-11). Critical bug and security fixes only, until **end of life on 2027-05-05**. Latest 9.7.7 (2026-07-17). Former Insiders features are now free. |
| **mkdocstrings** | Maintenance mode since late November 2025. Its author committed to about a year of maintenance from October 2025 and joined Zensical's core team to build native API documentation there. |
| **Zensical** | Material's team's ground-up successor. MIT core, commercial Studio product funding a full-time team. Reads `mkdocs.yml` directly. 0.0.71 on 2026-10-16, releasing about twice a week; 0.1.0 expected around 2026-11-05. No stability policy yet. |
| **ProperDocs** | A faithful MkDocs 1.6 fork by the last active MkDocs maintainer. One primary contributor; last release 1.6.7 (2026-03-20). Keeps MkDocs installable, does not carry a growing project. |
| **MaterialX** | Community continuation of Material 9.7.1, single author. Same position as ProperDocs, for the theme. |

## Why Zensical

- **It is where the people went.** The Material for MkDocs team and the
  mkdocstrings author now work on Zensical full time. Every other option is a
  fork maintained by one person.
- **The migration is a command swap, not a rewrite.** Zensical reads
  `mkdocs.yml`, keeps Python-Markdown and the `pymdownx.*` extensions, and
  preserves Material's HTML structure under the `classic` theme variant, so
  `extra.css` and `color-swatches.js` carry over.
- **The trial build matched.** On 2026-10-18, Zensical 0.0.71 built this site
  into the same 35 pages as MkDocs, strict mode clean, with the custom
  stylesheet and script linked, a search index generated, and the API
  reference rendered with its Attributes, Parameters, Returns, and Raises
  sections.
- **It covers the plugins this site uses.** `search` and `mkdocstrings` are
  native. Arbitrary MkDocs plugins and `hooks` are not run; this site uses
  neither.
- **Its roadmap covers what the next projects need.** Versioning (branches,
  tags, folders) and multi-project composition with cross-project links are
  planned. A combined site for DecisionLedger and the neobank repositories
  waits on that; until then each repository publishes its own site on the
  same theme with explicit cross-links.

## Why not something else

- **Sphinx (MyST, autodoc2, furo or shibuya).** The most durable choice and
  the only mature cross-repository linking story (intersphinx). It costs a
  rewrite of every page's Material-flavored Markdown, a new docstring
  toolchain, and the design system. The fallback if Zensical fails, not the
  plan.
- **Starlight, Docusaurus, Fumadocs.** JavaScript toolchains with no native
  Python docstring story; the API reference becomes a lossy export step.
- **Antora.** Multi-repository assembly is native, but AsciiDoc only.
- **Hosted platforms (Mintlify and similar).** Vendor lock-in for an
  open-source repository, with the same Python reference gap.

## The clocks

1. **mkdocstrings, around end of 2026.** The binding one. Eight reference
   pages, the Google-docstring rendering, and `griffe-pydantic` depend on it.
   Under Zensical it keeps working pinned at 1.x (verified in the trial);
   Zensical's native API documentation may replace it.
2. **Material for MkDocs, 2027-05-05.** The outer bound.

## What changed on 2026-10-18

- `mkdocstrings[python]` pinned to `>=1.0,<2` (was `<1`). Zensical requires
  pymdown-extensions 12, which mkdocstrings 0.30 cannot load; 1.x works with
  both builders.
- `mkdocs-material` upgraded in the lock to the latest 9.7.x security-fix
  release.
- `.github/workflows/docs.yaml` gained an advisory `zensical` job
  (`uv run --with zensical zensical build --strict`, `continue-on-error`).
  It installs the current Zensical release on purpose, so drift against
  upstream shows up on every docs push without blocking the deploy.

## Migration gate

Switch the production build when **all** of these hold:

- [ ] **Zensical 0.1.0 released** with a public changelog.
- [ ] **Three months of releases after 0.1.0** with no change that breaks
      the advisory CI build.
- [ ] **Advisory CI build green** on `main` for the same window.
- [ ] **Visual check** of the `classic` variant against the current site:
      palette, fonts, navigation tabs, code annotations, swatches.
- [ ] **mkdocstrings path decided**: stay on mkdocstrings 1.x under Zensical,
      or adopt Zensical's native API documentation once it covers Google
      docstrings and Pydantic models.

## Migration steps (when the gate opens)

1. Set `theme.variant: classic` in `mkdocs.yml` (ignored by MkDocs).
2. Replace `uv run mkdocs build --strict` with `uv run --with zensical
   zensical build --strict` in the `build` job; drop the advisory job; add
   `zensical` to the `docs` dependency group and pin it.
3. Rebuild locally, diff the page set against a MkDocs build, and open the
   reference pages.
4. Remove `mkdocs` and `mkdocs-material` from the `docs` group once the
   Zensical build has deployed cleanly. Keep `mkdocs.yml`; moving to
   `zensical.toml` is a separate, later change.

## Review schedule

| Date | Check |
|------|-------|
| 2026-05-06 | Zensical v0.0.40; drop-in compatibility confirmed upstream; deferred. |
| 2026-10-18 | Trial build matched MkDocs output with zero warnings. Pin bump, lock upgrade, advisory CI job added (above). Material end of life corrected to 2027-05-05. Next review after 0.1.0. |
| **2026-12-01** | Has 0.1.0 shipped? Is the advisory job green? What does the mkdocstrings maintenance notice say now? |
| **2027-02-01** | Gate check. If open, run the migration steps. If 0.1.0 has slipped, decide between waiting and pinning mkdocstrings 1.x for the remaining Material window. |

## What to watch

- [Zensical releases](https://github.com/zensical/zensical/releases) and
  [roadmap](https://zensical.org/roadmap/)
- [Zensical MkDocs plugin support](https://zensical.org/docs/compatibility/mkdocs/plugins/)
  and [migration guide](https://zensical.org/docs/compatibility/mkdocs/migration/)
- [Material for MkDocs changelog](https://squidfunk.github.io/mkdocs-material/changelog/)
  for the end-of-life date and security releases
- [mkdocstrings](https://mkdocstrings.github.io/) for the maintenance notice

## Current tooling (do not change without review)

```
mkdocs                     # core site generator — v1 branch
mkdocs-material            # theme — maintenance mode, EOL 2027-05-05
mkdocstrings[python] 1.x   # API reference; also runs under Zensical
griffe-pydantic            # Pydantic model rendering for mkdocstrings
pymdownx.*                 # superfences, highlight, tabbed, details,
                           # snippets, inlinehilite
docs/stylesheets/extra.css # custom theme overrides, color token system
docs/stylesheets/color-swatches.js  # hex/rgba swatch injection
```

The configuration lives in `mkdocs.yml` at the repo root. The advisory
Zensical build lives in `.github/workflows/docs.yaml`.
