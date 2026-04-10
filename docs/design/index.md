# DecisionLedger Design

```
docs/design
├── index.md
├── architecture.md          # C4 diagrams (L1–L3), directory layout, dependency rules
├── decisions.md             # DR-1–DR-15, cross-cuts everything
├── reasoner-handoff.md      # Domain ↔ framework handoff contract (read before touching features, scorer, or policy gate)
├── pipeline.md              # Runtime flow, latency budget, fallbacks
├── data.md                  # Schemas, corpus model, bundle structure
├── interface.md             # core/ boundary, contracts, API surfaces
├── evaluation.md            # 5-dimension eval framework, metrics, CI thresholds
├── policy-corpus.md         # Corpus documents, chunking strategy, jurisdiction design
└── infrastructure.md        # Docker Compose, Terraform, K8s target
```
