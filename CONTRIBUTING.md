# Contributing to DecisionLedger

Thanks for your interest in contributing.

The full contributing guide — local setup, development workflow, testing
philosophy, golden-dataset review rules, and the CI pipeline — lives in the
documentation site:

**[Contributing guide →](https://charleskelley.github.io/decision-ledger/development/contributing/)**

The short version:

1. Fork and clone the repository, then `uv sync` to install dependencies.
2. Run `make check` (lint + typecheck + unit tests) before pushing.
3. Use [Conventional Commits](https://www.conventionalcommits.org/) with the
   project's scopes (see the guide).
4. Open a pull request against `main` — CI is the safety net.

Note that integration and eval workflows require API secrets that are not
available to fork pull requests; maintainers will run those gates before merge.
