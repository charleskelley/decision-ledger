"""Shared fixtures for integration tests.

All integration tests require Docker services. Run with: make test-integration
"""

import uuid

import pytest
import redis


@pytest.fixture
def redis_client():
    """Connected Redis client. Closed after each test."""
    client = redis.Redis(host="localhost", port=6379, decode_responses=True)
    yield client
    client.close()


@pytest.fixture
def unique_stream_key():
    """Unique Redis Streams key per test to prevent cross-test interference."""
    return f"test:login-events:{uuid.uuid4().hex[:8]}"
