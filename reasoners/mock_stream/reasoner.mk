# =============================================================================
# Stream mock Make targets — included by the root Makefile via
#   -include $(wildcard reasoners/*/reasoner.mk)
# Recipes run from the repo root. Each target is suffixed with the reasoner
# directory name. The mock's corpus is loaded into the RAG policy gate, under
# the corpus id its binding names (mock-stream-practices, DR-34), by that
# gate's build-policy-index-<reasoner_id> target (gates/policy_rag/gate.mk), so
# `make prepare-reasoners` loads it with every other reasoner's. A reference-
# pattern mock (DR-35) never joins `make eval`: its eval is appended to
# MOCK_EVAL_TARGETS (`make eval-mocks`, Postgres only, no model calls).
# =============================================================================

MOCK_STREAM_REASONER_ID := mock-stream-reasoner
POLICY_CORPUS_ID_mock-stream-reasoner := mock-stream-practices
MOCK_STREAM_DIR := reasoners/mock_stream

.PHONY: prepare-mock_stream eval-mock_stream serve-mock_stream replay-mock_stream \
        registration-mock_stream assets-mock_stream

REASONER_INDEX_TARGETS   += build-policy-index-$(MOCK_STREAM_REASONER_ID)
REASONER_PREPARE_TARGETS += prepare-mock_stream
MOCK_EVAL_TARGETS        += eval-mock_stream

REASONER_HELP += "  mock_stream ($(MOCK_STREAM_REASONER_ID), a reference pattern: the real-time event scorer)"
REASONER_HELP += "    build-policy-index-$(MOCK_STREAM_REASONER_ID)  Load the mock's practice notes as mock-stream-practices (RAG policy gate)"
REASONER_HELP += "    prepare-mock_stream                 Index the corpus (the scorer needs no training)"
REASONER_HELP += "    eval-mock_stream                    Framework eval with the mock's adapter and gate double (part of eval-mocks)"
REASONER_HELP += "    serve-mock_stream                   Run the mock's service (port 8002; MOCK_STREAM_LEDGER_URL, _KEY)"
REASONER_HELP += "    replay-mock_stream                  Replay a synthetic stream into it (COUNT=200, SERVICE_URL)"
REASONER_HELP += "    registration-mock_stream            Regenerate $(MOCK_STREAM_DIR)/registration.yaml"
REASONER_HELP += "    assets-mock_stream                  Copy its prompts and corpus under ASSETS_ROOT (outputs/stage/assets)"

prepare-mock_stream: build-policy-index-$(MOCK_STREAM_REASONER_ID)

eval-mock_stream:
	uv run python -m eval.runners.harness --reasoner-id $(MOCK_STREAM_REASONER_ID)

# The mock's own service: it submits over HTTP to MOCK_STREAM_LEDGER_URL with
# MOCK_STREAM_LEDGER_KEY (a blank URL refuses to start).
serve-mock_stream:
	uv run python -m mock_stream_reasoner serve --port $(or $(PORT),8002)

replay-mock_stream:
	uv run python -m mock_stream_reasoner replay-events \
		--service-url $(or $(SERVICE_URL),http://localhost:8002) --count $(or $(COUNT),200)

registration-mock_stream:
	uv run python -m mock_stream_reasoner registration-yaml > $(MOCK_STREAM_DIR)/registration.yaml

# The mock's directory under a RAG policy gate asset root, for a ledger that
# registers it by file or over the API (its plugin excluded): point
# POLICY_RAG_GATE_ASSETS_ROOT at $(ASSETS_ROOT) (default outputs/stage/assets).
assets-mock_stream:
	@root="$(or $(ASSETS_ROOT),outputs/stage/assets)"; \
	rm -rf "$$root/$(MOCK_STREAM_REASONER_ID)" && mkdir -p "$$root/$(MOCK_STREAM_REASONER_ID)" && \
	cp -R $(MOCK_STREAM_DIR)/src/mock_stream_reasoner/assets/. "$$root/$(MOCK_STREAM_REASONER_ID)/" && \
	echo "assets-mock_stream: $$root/$(MOCK_STREAM_REASONER_ID):" && ls -R "$$root/$(MOCK_STREAM_REASONER_ID)"
