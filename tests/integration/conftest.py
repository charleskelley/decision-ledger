"""Shared fixtures for framework integration tests.

All integration tests require Docker services. Run with: make test-integration

Framework integration tests use Postgres and Elasticsearch only. Reasoner
infrastructure (for example the ATO reasoner's Redis) and its fixtures live
with that reasoner's tests under ``tests/reasoner/``.
"""
