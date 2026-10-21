# Registration

A reasoner's registration is what the ledger knows about it: its gate set
and the configuration of each binding, its bypass rules, the actions it may
record, the native namespaces it names subjects in, and its enforcement
profile. It is data: intake validates every submission against it,
enforcement reads its profile, and replay reads the snapshot of the version
a bundle cites. A reasoner can be registered with no plugin installed in the
ledger: by the deployment's registration file, or over the API by the
operator. The record is
[DR-33](decisions.md#dr-33-registration-is-a-writable-versioned-resource-bindings-carry-gate-validated-config);
the code is `core/observation/registration.py` (the contract),
`app/reasoner_registry.py` (the live registry), `app/plugins.py:build_registry`
(composition), and `app/api/registrations.py` (the resource).

## Three sources, one registry

| Source | How | `source` label | Changes when |
|---|---|---|---|
| Plugin | `ReasonerPlugin.registration`, published under `decisionledger.reasoners` | `plugin:<module>` | The deployment installs another version |
| File | An entry in `DECISIONLEDGER_REGISTRATIONS` (YAML) | `file:<path>` | The ledger restarts with a new file |
| API | `PUT /api/v1/reasoners/{reasoner_id}` with the operator key | `api` | Another `PUT` replaces it |

All three produce the same `ReasonerRegistration`, the same
`registration_version`, and the same snapshot row. A reasoner has one
source: a `reasoner_id` two sources register refuses startup
(`RegistrationError`), and a `PUT` for a plugin's or the file's reasoner
answers `409 REGISTRATION_SOURCE_IMMUTABLE`, because declared configuration
stays declared. `DECISIONLEDGER_REASONERS` filters the installed plugins
only; the file's and the API's registrations are always served.

## Writing a registration

```bash
curl -X PUT "$DECISIONLEDGER_URL/api/v1/reasoners/example-reasoner" \
  -H "X-DecisionLedger-Key: $DECISIONLEDGER_OPERATOR_KEY" \
  -H "content-type: application/json" \
  --data @registration.json
```

The body is one registration in the JSON shape of a registration-file entry
(`conformance/registrations.yaml` is the example reasoner's;
`conformance/examples/registration.json` is a minimal one), parsed strictly.
Its `reasoner_id` must be the one the path names. In Python,
`LedgerClient.register(registration)` sends it: `HttpClient` with its
operator key, `LocalClient` as its principal (the trusted in-process one by
default). Registering is an operator act: a reasoner key answers
`403 OPERATOR_REQUIRED`, and the MCP server does not register.

| Answer | When |
|---|---|
| `201` with `Location` | The reasoner was not registered: its registration is created |
| `200` | Its API registration was replaced by a new version, or is unchanged (same version; nothing is written) |
| `401 UNAUTHENTICATED` | No key, or an unknown one |
| `403 OPERATOR_REQUIRED` | A reasoner's key |
| `409 REGISTRATION_SOURCE_IMMUTABLE` | A plugin or the registration file registers the reasoner (`source` says which) |
| `413 BODY_TOO_LARGE` | The body is over 1 MiB |
| `422 REGISTRATION_INVALID` | The body is not a valid registration, registers another reasoner than the path names, or holds what the ledger cannot store as written (a NUL, a non-finite number, `-0.0`, or a float written with an exponent, 1e16 and up: Postgres `jsonb` would store another number, and the snapshot another version) |
| `422 NATIVE_NAMESPACE_CONFLICT` | Another registration declares one of its native namespaces differently |
| `422 GATE_BINDING_REFUSED` | The composed gates cannot serve its gate set; `gate_id` names the gate and `detail` says why |

Every answer that is not an error is the registration as served now
(`RegisteredReasonerView`): `reasoner_id`, `reasoner_name`,
`registration_version`, `source`, `registered_by` (who wrote the version
served now, a principal label such as `operator`), `registered_at` (when the
reasoner was first registered over the API), `updated_at` (when the version
served now was written), and `registration` in its canonical form. A
plugin's or the file's registration has no `registered_by`,
`registered_at`, or `updated_at`. `GET /api/v1/reasoners/{reasoner_id}`
serves the same view.

## Admission is the startup check

A write is admitted exactly as a registration is when the deployment
starts, and a refusal stores nothing:

1. The body validates as a `ReasonerRegistration` (gate bound once, rule ids
   unique, every rule's action recordable, each namespace declared once).
2. No other registration the deployment serves declares one of its native
   namespaces differently (the one it replaces is not compared with it).
3. Every gate it binds as mandatory is composed. An advisory binding to a
   gate the deployment does not compose is accepted and logged, as at
   startup: that reasoner's gated decisions fail closed at that gate.
4. Every composed gate it binds admits the binding (`app.gates.check_gate_set`):
   the plugin's `binding_config_model`, when it declares one, validates the
   binding's `config`, then a gate that is a `BindingValidator` accepts or
   refuses the binding (`GateConfigError` with the reason), and then a gate
   that is a `ReasonerScope` must serve the reasoner. A gate with neither a
   model nor a validator takes no configuration: a binding to it with one is
   refused.
5. Every gate the deployment mandates (`DECISIONLEDGER_MANDATORY_GATES`) and
   the registration does not bind accepts the empty configuration it will
   run with.

Writes are serialized: the registry's lock is held across the admission and
the write, so two writes never admit against the same state. The checks
that ask no gate anything (steps 1 to 3 and 5) run before any gate admits a
binding. A gate's admission is add-only: when one gate admits a binding and
another then refuses the write, nothing is stored, but what the first gate
learned stays until the ledger restarts (it decides nothing, since intake
serves registered reasoners only; the RAG gate keeps the reasoner's
template ids registered, so another reasoner shipping one of them is
refused until then).

## Versions, snapshots, and replay

A new version's snapshot and the reasoner's row are written in one
statement before the version is served, so every bundle's
`registration_version` has a snapshot. A decision already deciding keeps
the registration it read; the next one reads the new version. Replay
enforces every bundle against the snapshot of the version it cites, so a
decision made under a replaced version replays as it was made.
`GET /api/v1/reasoners/{reasoner_id}/registrations/{version}` serves every
stored version.

## Binding configuration

`GateBinding.config` (default `{}`) is how a gate is configured for one
reasoner: JSON the framework never interprets, part of the registration, so
versioned and snapshotted with it. A binding without configuration has no
`config` key in the canonical form, so registrations whose bindings carry
none kept the versions they had before DR-33. The order of a
configuration's keys never changes the version; the order of a list in it
does. What a single decision asks of a gate stays in that submission's
`GateContext.gate_config`.

```yaml
- reasoner_id: example-reasoner
  reasoner_name: Example Reasoner
  gates:
  - gate_id: policy
    mandatory: false
    config: {}        # the RAG policy gate takes no binding configuration yet
```

The gate validates the configuration when it admits the binding. A gate
declares the shape it accepts as its plugin's `binding_config_model`; the
RAG policy gate declares none and refuses a non-empty configuration until
its corpus references arrive (DR-34).

## A reasoner without a plugin, and its gate assets

A gate that serves reasoners from per-reasoner assets reads a reasoner
without a plugin from where that gate documents, when it admits the
reasoner's binding. The RAG policy gate reads them from its asset root,
`POLICY_RAG_GATE_ASSETS_ROOT`:

```text
$POLICY_RAG_GATE_ASSETS_ROOT/
└── <reasoner_id>/
    ├── prompts/*.yaml      prompt templates (template ids are global)
    └── corpus/**/*.md      policy documents with front matter
```

The gate reads a reasoner's directory once, the first time it admits the
reasoner's binding (at startup for a file or API registration, or when the
registration is written): its templates join the prompt registry and its
profile (template ids, the jurisdictions its corpus covers) is added. It is
add-only: a template id the deployment already registers refuses the
binding, nothing is removed, and a template added later to an admitted
reasoner's directory is read at the next start. A reasoner the deployment
composes as a plugin is served from its plugin; its directory under the
root, if any, is not read. The gate refuses a reasoner whose directory is
missing, holds no template or a malformed one, or no policy document whose
front matter parses.

The corpus loader indexes the same directory, after the reasoner is
registered:

```bash
uv run python -m policy_rag_gate.corpus_loader --reasoner-id <reasoner_id>
```

It resolves the corpus as the gate reads it: the composed plugin's
`corpus_dir` (plugins filtered by `DECISIONLEDGER_REASONERS`), else
`<root>/<reasoner_id>/corpus`; an explicit `--corpus-dir` overrides both. It
refuses a `--reasoner-id` the ledger's registry does not know (plugins, the
registration file, and the API's registrations, read from the ledger's
Postgres) before it loads a model. It composes that registry as the ledger
does, so run it with the ledger's `DECISIONLEDGER_REASONERS`,
`DECISIONLEDGER_REGISTRATIONS`, and `POLICY_RAG_GATE_ASSETS_ROOT` (in `.env`,
or on the command line). So the order for a new reasoner is: assets under
the root, register, load the corpus, decide.

## Persistence and restart

`decisionledger.reasoner_registry` holds one row per reasoner registered
over the API: `reasoner_id`, `registration_version` (a foreign key into
`decisionledger.reasoner_registrations`, which holds the registration
itself), `source` (`api`), `registered_by`, `registered_at`, and
`updated_at`. It is the source of truth on restart: the bootstrap composes
its rows with the plugins' and the file's registrations and admits each one
again with the gates, and a reasoner a plugin or the file also registers
refuses startup. Every process that starts the ledger on that database (a
restart, `LocalClient.from_settings`, an eval driver) does the same, so it
needs what the gates need to admit those registrations (for the RAG gate,
`POLICY_RAG_GATE_ASSETS_ROOT`). `app.bootstrap.read_registry` composes the
same registry, read only, for a tool that runs beside the ledger.

## Keys

Registering issues no key. A reasoner's key is deployment configuration,
in `DECISIONLEDGER_API_KEYS` (`reasoner_id:key` pairs) or the key file
`DECISIONLEDGER_API_KEYS_FILE`, and the ledger reads its keys when it
starts: provision a new reasoner's key with the deployment, or add it and
restart. A key for a reasoner that is not registered yet is allowed.

## Not possible, by design (deferred)

- **Deleting a registration.** Bundles reference registrations; there is no
  `DELETE`. To take a reasoner out of service, revoke its key.
- **Issuing keys over the API.** Keys stay deployment configuration.
- **Uploading prompts or a corpus.** The asset root and the corpus loader
  cover deployment; an upload API would reverse the load-once prompt
  registry.

In a development database, an API registration you no longer want (for
example one that now collides with a plugin) is removed with SQL; its
snapshots stay, so the bundles that cite them still replay:

```sql
delete from decisionledger.reasoner_registry where reasoner_id = 'example-reasoner';
```
