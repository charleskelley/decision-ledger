Review the current git diff (both staged and unstaged). Stage all relevant changes with `git add`. Then suggest a Conventional Commits message following this format:

```
<type>(<scope>): <short description>

<optional body explaining what changed and why>
```

Types: feat, fix, docs, test, refactor, build, ci, chore
Scopes: core, ingestion, features, scorer, retrieval, policy-gate, enforcement, audit, eval, scenarios, infra, ci

Present the suggested commit message and the list of staged files. Do NOT run `git commit` — the user will do that.
