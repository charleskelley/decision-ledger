"""Fixtures for ATO integration tests that touch the reasoner's Redis.

Redis is ATO reasoner infrastructure (feature store and event queue), so its
fixtures live here rather than in the framework's ``tests/integration/``.
Requires Docker services. Run with: make test-integration
"""

import uuid

import pytest
import redis

from ato_reasoner.settings import AtoSettings


@pytest.fixture
def redis_client():
    """Connected Redis client (``AtoSettings().redis_url``). Closed after each test."""
    client = redis.Redis.from_url(AtoSettings().redis_url, decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def unique_stream_key():
    """Unique Redis Streams key per test to prevent cross-test interference."""
    return f"test:login-events:{uuid.uuid4().hex[:8]}"
