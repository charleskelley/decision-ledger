# Documentation Tooling: MkDocs → Zensical Migration

!!! warning "Planned future migration — no action required now"
    This document describes a migration that is **not happening yet**. The current
    MkDocs + Material setup is stable and sufficient for the MVP phase. Read this if
    you are evaluating docs tooling or want to understand the ecosystem situation.

---

## Background

MkDocs, the documentation tool powering this site, collapsed as a maintained
open-source project in early 2026. The failure was not technical — the software
still works — but organizational: a decade of maintainer turnover, a prolonged
conflict between the project's primary maintainer and the author of Material for
MkDocs (by far the most popular theme), and a v2 redesign that stripped plugin
support and was developed in a private repository under a corporate umbrella
against the wishes of the existing community.

The crisis came to a head in March 2026, when the ousted former maintainer used
retained PyPI publishing access to remove all other maintainers from the
`mkdocs` package and launch a v1 fork called ProperDocs. He reversed course
within 24 hours, but the episode confirmed what had been building for two
years: the original repository is effectively stalled, and the ecosystem has
fragmented into three competing successors:

| Successor | Led by | Approach |
|-----------|--------|----------|
| **ProperDocs** | Former MkDocs maintainer (@oprypin) | v1 fork, preserves existing plugin API |
| **MaterialX** | Community | Material theme continuation |
| **Zensical** | squidfunk (Material for MkDocs author) | Ground-up rewrite |

## Why Zensical Is the Target

Zensical is the successor most worth watching for this project:

- It is led by squidfunk, who built Material for MkDocs — the theme this site
  already uses. The custom CSS, color system, and theme features in this repo
  were written against Material's token and feature model. Zensical is
  effectively Material's next host framework rather than a departure from it.
- As of March 2026 it has over 3,700 GitHub stars and is the most actively
  developed of the three forks, despite being the newest.
- ProperDocs preserves the old plugin API but its lead maintainer explicitly
  cited isolation and burnout as reasons for stepping down from MkDocs — not
  a strong sustainability signal for a fork he is now running solo.
- The original MkDocs v2 direction (private development, plugin removal) is
  the worst outcome for this project's needs and is not being tracked.

## Why We Are Not Migrating Now

Zensical is too new. A ground-up rewrite that has been public for less than a
year does not yet have the stability guarantees, plugin ecosystem, or community
surface area needed to justify migrating a working documentation setup during
an MVP development phase. The migration cost is not zero:

- **PyMdown Extensions** (`pymdownx.superfences`, `pymdownx.highlight`,
  `pymdownx.tabbed`, `pymdownx.details`, `pymdownx.snippets`,
  `pymdownx.inlinehilite`) are a separate dependency from the theme. Zensical
  needs to document stable compatibility with this extension set before
  migration is viable.
- The custom JavaScript (`color-swatches.js`) and CSS (`extra.css`) were
  written against Material's CSS variable model and theme API. Until Zensical
  publishes a stable theme API, rewriting these against a moving target is
  wasteful.
- MkDocs Material continues to work on the current MkDocs v1 baseline. There
  is no breakage to fix — this is a proactive migration planning exercise, not
  an emergency.

The right time to migrate is when Zensical has earned trust, not when MkDocs
has failed enough to force our hand.

## Stability Targets for Migration

Migration becomes reasonable when **all** of the following are true:

- [ ] **Zensical 1.0 stable release** — a versioned release with a public
      changelog and a stated stability policy (no breaking changes without a
      deprecation period). Pre-1.0 releases do not count.
- [ ] **PyMdown Extensions compatibility** — official documentation or a
      confirmed community migration path for the full `pymdownx.*` extension
      set used in `mkdocs.yml`. Specifically: `superfences`, `highlight`,
      `tabbed`, `details`, `snippets`, `inlinehilite`.
- [ ] **Theme customization API** — a documented, stable API for custom CSS
      variables and JavaScript hooks equivalent to what Material for MkDocs
      provides today. The color token system in `extra.css` and the
      `color-swatches.js` hook both depend on this.
- [ ] **Plugin ecosystem parity** — the `search` plugin (or a first-party
      equivalent) ships with feature parity including instant navigation,
      search highlighting, and search sharing.
- [ ] **3+ months of stable releases** — at least three months between the
      1.0 release and the migration attempt, with no breaking changes in that
      window. This gives the community time to surface regressions before
      we commit migration effort.

## Review Schedule

Check the following at each interval and update this document with findings:

| Date | Check |
|------|-------|
| **2026-05-06** | Status check (no migration). Latest Zensical release is **v0.0.40** (2026-05-04); weekly release cadence with ongoing API changes; no public ETA for 1.0. Drop-in compatibility with `mkdocs.yml`, the custom `extra.css` / `color-swatches.js` (HTML structure preserved), and `pymdownx.*` (Python Markdown extensions still work) is confirmed by upstream — so waiting costs nothing. Bar not met: 1.0 missing and no 3-month stability window. Defer per existing schedule; next review 2026-06-01. |
| **2026-06-01** | Has Zensical published a 1.0 release candidate or roadmap? Has the PyMdown Extensions compatibility story clarified? Is ProperDocs still actively maintained, or has it stalled? |
| **2026-09-01** | Has Zensical 1.0 shipped? Are any early adopters in the Material community reporting stable migrations? |
| **2026-12-01** | Full evaluation against all stability targets above. If all targets are met, draft a migration plan for the following quarter. |

If Zensical ships a stable 1.0 with documented PyMdown compatibility before
the September check, pull the evaluation forward — do not wait for a calendar
date if the evidence is already there.

## What to Watch

- [Zensical GitHub repository](https://github.com/zensical/zensical) — star
  trajectory, release cadence, issue closure rate
- [Material for MkDocs GitHub Discussions](https://github.com/squidfunk/mkdocs-material/discussions) —
  squidfunk's announcements and migration guidance will appear here before
  anywhere else
- [PyMdown Extensions repository](https://github.com/facelessuser/pymdown-extensions) —
  watch for any compatibility announcements or migration guides targeting
  Zensical
- The ProperDocs fork — if it gains significant traction it becomes a lower-
  risk migration path (same plugin API, no rewrite), but its bus-factor is
  currently one person

## Current Tooling (Do Not Change Without Review)

For reference, the current docs stack this migration plan applies to:

```
mkdocs                     # core site generator — v1 branch
mkdocs-material            # theme — Material for MkDocs by squidfunk
pymdownx.*                 # superfences, highlight, tabbed, details,
                           # snippets, inlinehilite
docs/stylesheets/extra.css # custom theme overrides, color token system
docs/stylesheets/color-swatches.js  # hex/rgba swatch injection
```

The configuration lives in `mkdocs.yml` at the repo root.
