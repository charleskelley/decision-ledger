"""Idempotent event ingestion from Redis Streams.

``consumer`` implements deduplication via SHA-256 idempotency key, bounded
lateness handling (events older than configurable window routed to
dead-letter queue), and schema validation against
``ato_reasoner.events.LoginEvent`` before consumer group acknowledgment.
``stream`` is the async adapter that decides each accepted event through
ATO's pipeline (``python -m ato_reasoner consume``).
"""
