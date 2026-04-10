"""Idempotent event ingestion from Redis Streams.

Implements deduplication via SHA-256 idempotency key, bounded lateness handling
(events older than configurable window routed to dead-letter queue), and schema
validation against core.decision.LoginEvent before consumer group acknowledgment.
"""
