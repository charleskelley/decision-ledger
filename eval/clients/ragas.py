"""``RagasFaithfulnessScorer`` implementation backed by RAGAS.

The RAGAS library and its (LangChain-based) LLM/embeddings dependencies are
imported lazily so the eval harness's unit-test layer never has to load
them. The dimension under test stubs the scorer; this adapter is exercised
in the live eval run only.

RAGAS itself depends transitively on LangChain — accepted because RAGAS
implements paper-backed faithfulness scoring (claim decomposition +
entailment) we'd otherwise have to reproduce. The coupling is encapsulated
here; nothing in ``eval/dimensions/`` imports RAGAS directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class RagasFaithfulnessAdapter:
    """Adapter wrapping RAGAS' ``faithfulness`` metric.

    Args:
        model: OpenAI chat model identifier RAGAS' internal LLM uses.
        embedding_model: Embedding model identifier (RAGAS uses one
            internally for some metrics).

    The first ``score()`` call lazily imports RAGAS and constructs the
    underlying metric object — keeps test-time imports light.
    """

    def __init__(
        self,
        *,
        model: str = "gpt-4o-2024-08-06",
        embedding_model: str = "text-embedding-3-small",
    ) -> None:
        """Initialise the adapter (no RAGAS work yet)."""
        self._model = model
        self._embedding_model = embedding_model
        self._metric = None  # populated on first call

    async def score(self, *, rationale: str, contexts: Sequence[str]) -> float:
        """Score one (rationale, contexts) pair via RAGAS.

        The RAGAS ``faithfulness`` metric decomposes the rationale into
        atomic claims and checks each against the contexts using its
        configured LLM. Returns the mean fraction of claims supported.

        Args:
            rationale: Gate rationale text.
            contexts: Retrieved policy snippets the gate had access to.

        Returns:
            Faithfulness score in ``[0, 1]``.
        """
        metric = self._get_or_init_metric()
        sample = self._build_sample(rationale=rationale, contexts=list(contexts))
        score = await metric.single_turn_ascore(sample)
        return float(score)

    def _get_or_init_metric(self):
        """Lazy-init the RAGAS faithfulness metric with our LLM/embeddings."""
        if self._metric is not None:
            return self._metric

        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import Faithfulness

        llm = LangchainLLMWrapper(ChatOpenAI(model=self._model))
        embeddings = LangchainEmbeddingsWrapper(
            OpenAIEmbeddings(model=self._embedding_model)
        )
        self._metric = Faithfulness(llm=llm, embeddings=embeddings)
        return self._metric

    @staticmethod
    def _build_sample(*, rationale: str, contexts: list[str]):
        """Construct a RAGAS SingleTurnSample for the metric."""
        from ragas.dataset_schema import SingleTurnSample

        return SingleTurnSample(
            user_input=(
                "Evaluate the policy gate's rationale against retrieved evidence."
            ),
            response=rationale,
            retrieved_contexts=contexts,
        )
