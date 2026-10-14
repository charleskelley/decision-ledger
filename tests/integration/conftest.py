"""Shared fixtures for framework integration tests.

All integration tests require Docker services. Run with: make test-integration

Framework integration tests use Postgres only, the framework's one piece of
infrastructure. Plugin infrastructure and its fixtures live with that
plugin's tests: the RAG policy gate's Elasticsearch and ``policy_rag`` schema
under ``gates/policy_rag/tests/integration/``, the ATO reasoner's Redis under
``reasoners/account_takeover/tests/integration/``.
"""
