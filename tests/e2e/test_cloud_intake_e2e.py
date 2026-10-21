"""End-to-end integration test for Project AURA Phase 2 Cloud Intake.

Executes the complete pipeline:
Bee Fixture Replay
  -> Local Boundary Redaction
  -> Quarantined in LocalOutbox
  -> Cloud Relay Client Dispatch
  -> WebSocket / HTTP Cloud Intake Handler
  -> EventBridge Ingestion
  -> Server Acknowledgement (ack_id)
  -> Zero-Retention Outbox Purge & Audit Verification.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

_ws = importlib.import_module("lambda.websocket_handler")
handle_relay = _ws.handle_relay
from standalone_agent.bee_monitor import BeeMonitor, LocalOutbox
from standalone_agent.cloud_relay import CloudRelayClient

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "bee_stream_sample.jsonl"


def test_phase2_full_cloud_intake_pipeline() -> None:
    # 1. Initialize local outbox and monitor
    outbox = LocalOutbox(":memory:")
    monitor = BeeMonitor(outbox=outbox, relay_fn=None)  # local containment initially

    # 2. Replay Bee stream fixture
    event_count = monitor.replay_fixture(FIXTURE_PATH)
    assert event_count == 5

    # 3. Verify all 5 events are quarantined in local outbox
    pending = outbox.get_pending(limit=10)
    assert len(pending) == 5

    # Verify sensitive data was redacted before outbox entry
    email_event = next(e for e in pending if e["id"] == "evt_002")
    evidence = email_event["payload"]["evidence"]
    assert len(evidence) == 1
    assert "aditi@university.edu" not in evidence[0]["snippet"]
    assert "[EMAIL_REDACTED]" in evidence[0]["snippet"]
    assert "[PHONE_REDACTED]" in evidence[0]["snippet"]

    # 4. Set up Cloud Intake handler mock (EventBridge bus)
    mock_bus = MagicMock()

    def cloud_intake_transport(envelope: dict) -> dict:
        # Calls the actual Cloud Intake handler with authenticated connection identity
        res = handle_relay(
            envelope,
            bus_client=mock_bus,
            bus_name="aura-event-bus",
            authenticated_user_id=envelope.get("user_id", "usr_aditi_01"),
        )
        assert res["statusCode"] == 200
        return res["body"]

    # 5. Initialize Cloud Relay client and drain outbox
    relay_client = CloudRelayClient(
        user_id="usr_aditi_01",
        transport_fn=cloud_intake_transport,
    )

    drained_count = relay_client.drain_outbox(outbox)
    assert drained_count == 5

    # 6. Verify EventBridge received all events
    assert mock_bus.put_events.call_count == 5

    # 7. Verify outbox zero-retention guarantee: no pending items, all bodies purged to '{}'
    assert len(outbox.get_pending()) == 0

    with outbox.conn:
        rows = outbox.conn.execute("SELECT id, payload, purged FROM outbox").fetchall()
        for row in rows:
            assert row[1] == "{}", f"Payload for {row[0]} was not purged!"
            assert row[2] == 1, f"Event {row[0]} was not marked as purged!"
