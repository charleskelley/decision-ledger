# Secrets management

DecisionLedger reads every secret from environment variables, with a
single read site at `app/settings.py:Settings`. Where those env vars come
from is a deployment concern, not an application concern. This page
covers the three deployment modes the project targets: local development,
CI/CD, and production cloud.

## Required secrets

| Variable             | Purpose                                                                | Required for                        |
| -------------------- | ---------------------------------------------------------------------- | ----------------------------------- |
| `OPENAI_API_KEY`     | Policy gate calls (PolicyGate consumes OpenAILLMClient).               | Any pipeline run, `make test-smoke` |
| `ANTHROPIC_API_KEY`  | Eval-harness faithfulness/citation judges (cross-family bias control). | `make eval`                         |

Infrastructure values (Postgres credentials, Redis host/port, Elasticsearch
URL) also flow through `Settings`, but their defaults match `docker-compose.yml`
so they rarely need overriding. See `.env.example` for the complete list.

## Architecture: one read site, many sources

```
                       env vars
                          │
                          ▼
          ┌───────────────────────────────┐
          │  app/settings.py:Settings     │
          │  (pydantic_settings)          │
          └──────────────┬────────────────┘
                         │ field accessors
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
  PolicyGate        PipelineDriver    Eval harness
```

The application is unchanged across deployment modes. Only the *source* of
the env vars differs. This is the 12-factor pattern — and it's what makes
a single codebase portable across Docker, GitHub Actions, AWS ECS, GCP
Cloud Run, and Azure Container Apps.

---

## Mode 1: Local development

For reviewers cloning the repo and running it on their own machine.

1. **Copy the template:**
   ```bash
   cp .env.example .env
   ```
2. **Fill in the keys** in `.env`:
   - `OPENAI_API_KEY` (https://platform.openai.com/api-keys)
   - `ANTHROPIC_API_KEY` (https://console.anthropic.com/settings/keys)
3. **Loading.** Three independent consumers each read `.env`:
   - **Docker Compose** reads `.env` automatically for `${VAR}`
     substitution in `docker-compose.yml`.
   - **`pydantic_settings`** reads `.env` from `app/settings.py` so
     `make eval`, `make test-smoke`, and the FastAPI lifespan see the
     keys without manual `export`.
   - **direnv** (`.envrc`) calls `dotenv` to export every var into the
     shell, so direct `uv run` invocations also see them on `cd`.
4. **`make test-smoke`** and **`make eval`** consume the same env via
   `Settings()`.

`.env` is gitignored. `.env.example` is committed and serves as the
documented contract — every field on `Settings` should appear there.

> **Migrating from `.env.local`?** Earlier setups sometimes used
> `.env.local` (a Next.js convention). DecisionLedger standardizes on
> `.env`: rename your existing `.env.local` to `.env`, or copy its
> contents over.

### Pre-commit guard (gitleaks)

`gitleaks` runs as a pre-commit hook (configured in
`.pre-commit-config.yaml`) and blocks commits that contain anything
matching its rule set for API keys, AWS credentials, JWTs, etc. Install
once after cloning:

```bash
make install-hooks   # or: uv run pre-commit install
```

If gitleaks blocks a legitimate value (rare — usually only false
positives on test fixtures), redact the value or move it into a fixture
file gitleaks can be configured to skip. Never bypass with
`--no-verify`; that defeats the entire purpose.

---

## Mode 2: CI/CD (GitHub Actions)

Three workflows under `.github/workflows/`:

| Workflow            | Trigger                          | Secrets needed                               |
| ------------------- | -------------------------------- | -------------------------------------------- |
| `ci.yaml`           | every push and PR                | none — runs on forks                         |
| `integration.yaml`  | PRs to `main`                    | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`        |
| `eval.yaml`         | manual + nightly cron            | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`        |

### Configuring secrets

GitHub repo → Settings → Secrets and variables → Actions → New repository
secret. Add `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`. The workflows
reference them via `${{ secrets.NAME }}` and inject as job-level env vars.

### Environment-scoped secrets

The `eval.yaml` workflow runs under a GitHub Actions *environment* named
`eval`. Configure environment-scoped secrets there (Settings →
Environments → eval) so the eval workflow's API spend is scoped
separately from the integration workflow. This lets you set:

- Distinct keys for eval vs integration if budgets differ.
- Required reviewers on the `eval` environment so manual triggers
  require approval.

### Why `ci.yaml` carries no secrets

The `quality` and `boundary` jobs in `ci.yaml` perform static analysis
only — ruff, sqlfluff, pyright, unit tests, and the DR-23 SDK-import
boundary check. None of these touch an LLM provider. Keeping `ci.yaml`
secret-free means it runs successfully on PRs from forks, where secrets
are unavailable.

---

## Mode 3: Production cloud

Production secrets live in a managed secrets store and are pulled by the
compute runtime via IAM-bound identity. The application code never sees
long-lived API keys baked into images.

### AWS (primary target)

**Components:**

- **AWS Secrets Manager** stores `openai_api_key`, `anthropic_api_key`,
  Postgres credentials, etc.
- **ECS task definition** (or App Runner / Lambda equivalent) declares
  which secrets to inject as env vars at boot.
- **IAM task role** grants `secretsmanager:GetSecretValue` on the
  specific ARNs the task needs — least privilege.

**ECS task definition snippet (excerpt):**

```json
{
  "family": "decisionledger",
  "containerDefinitions": [{
    "name": "decisionledger",
    "image": "<your-ecr-repo>:latest",
    "secrets": [
      {
        "name": "OPENAI_API_KEY",
        "valueFrom": "arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:dl/openai-api-key-XXXXXX"
      },
      {
        "name": "ANTHROPIC_API_KEY",
        "valueFrom": "arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:dl/anthropic-api-key-XXXXXX"
      }
    ],
    "environment": [
      {"name": "POSTGRES_HOST", "value": "<rds-endpoint>"},
      {"name": "ELASTICSEARCH_URL", "value": "<opensearch-endpoint>"}
    ]
  }],
  "taskRoleArn": "arn:aws:iam::ACCOUNT:role/decisionledger-task-role"
}
```

ECS reads each `secrets` entry at container start and injects the
resolved value as an env var of the named key. The application boots and
`Settings()` reads `OPENAI_API_KEY` from the environment exactly the
same way it does locally.

**IAM task role policy (least privilege):**

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": [
      "arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:dl/openai-api-key-*",
      "arn:aws:secretsmanager:us-east-1:ACCOUNT:secret:dl/anthropic-api-key-*"
    ]
  }]
}
```

### Other clouds

The pattern is portable. Application code is unchanged.

| Cloud   | Secrets store     | Container runtime injector                            |
| ------- | ----------------- | ----------------------------------------------------- |
| AWS     | Secrets Manager   | ECS task definition `secrets:` block                  |
| GCP     | Secret Manager    | Cloud Run `--set-secrets KEY=secret-name:latest`      |
| Azure   | Key Vault         | Container App `secretRef` in `env` array              |
| Generic | HashiCorp Vault   | Vault Agent sidecar writes env vars or files at boot  |

### Rotation policy

Rotate API keys quarterly, or immediately on suspected exposure.

1. Generate a new key from the provider console.
2. Update the secret value in your secrets store (Secrets Manager,
   etc.). For AWS Secrets Manager: `aws secretsmanager
   update-secret --secret-id dl/openai-api-key --secret-string '<new>'`.
3. Restart or redeploy the running tasks to pick up the new value
   (`aws ecs update-service --force-new-deployment ...`).
4. Verify `make test-smoke` against the rotated stack before deleting
   the old key from the provider.
5. Delete the old key from the provider's console.

The application has no key-cache to invalidate — a fresh container reads
the env var once at boot.

---

## Public-demo cost controls (post-polish)

A public URL backed by your OpenAI key is a billing-attack surface. When
the polish-phase live demo ships, layer three controls:

### 1. Auth at the edge

A shared bearer token, gated through API Gateway / CloudFront. Reviewers
get the token via the README; casual scrapers don't. Even a weak gate
filters 95% of opportunistic abuse.

### 2. Per-IP rate limiting

CloudFront or API Gateway throttles requests to N/minute per source IP.
Caps amplification: a compromised token can spike for a minute, not
saturate your monthly OpenAI budget.

### 3. Budget cap with auto-disable

CloudWatch alarm on the OpenAI/Anthropic billing CloudWatch metric (or a
custom metric you publish from the app's token-usage logs). Alarm
threshold at, say, 80% of daily budget; alarm action triggers a Lambda
that toggles a feature flag (or Secrets Manager value) the demo reads
at request time. App returns "demo paused" until the budget resets.

### Per-call cost monitoring

The pipeline already records `TokenCost` on every gate call (see
`app/llm/_pricing.py`). For the live demo, scrape these metrics into
CloudWatch (via a structured-log ingestion path) so you have per-day,
per-scenario, per-user spend visibility.

---

## Verifying secrets discipline

Run these checks as part of release readiness:

```bash
# 1. Pre-commit hooks installed
ls .git/hooks/pre-commit | grep -q . && echo "pre-commit installed"

# 2. Gitleaks scan clean
uv run pre-commit run gitleaks --all-files

# 3. .env not tracked
git check-ignore .env && echo ".env is gitignored"

# 4. .env.example covers every Settings field
diff <(grep -oE '^\s*[a-z_]+: ' app/settings.py | sed 's/[: ]//g' | sort -u) \
     <(grep -oE '^[A-Z_]+=' .env.example | sed 's/=//' | tr 'A-Z' 'a-z' | sort -u)

# 5. CI boundary check (DR-23) catches accidental SDK leaks
! grep -rE "^(from|import) (openai|anthropic)" --include="*.py" \
    app/ core/ eval/ reasoner/ tests/ | grep -v "^app/llm/"
```

Failing any of these means a secrets-discipline regression has landed.
The CI workflows enforce checks 2 and 5 automatically.
