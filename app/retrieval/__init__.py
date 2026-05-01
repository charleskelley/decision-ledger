"""Hybrid policy RAG retriever — pgvector dense + Elasticsearch BM25 + reranking.

Implements RRF (Reciprocal Rank Fusion) of dense and sparse result lists,
followed by cross-encoder reranking with a configurable latency-budget
bypass. Metadata filters enforce jurisdiction and version preferences.
Returns top-k ``RetrievedSnippet`` objects.
"""
