"""Unit tests for Phase 5 Security, Consent Enforcement, and Undo Replay Protection."""
from __future__ import annotations

import importlib
import json
import time
from typing import Any
import pytest

cp = importlib.import_module("lambda.control_plane")
pe = importlib.import_module("standalone_agent.policy_engine")

InMemoryDataStore = cp.InMemoryDataStore
handle_undo_action = cp.handle_undo_action
handle_create_grant = cp.handle_create_grant
handle_revoke_grant = cp.handle_revoke_grant
handle_focus_signal = cp.handle_focus_signal

PolicyEngine = pe.PolicyEngine
ConsentGrant = pe.ConsentGrant


@pytest.fixture
def clean_store() -> InMemoryDataStore:
    return InMemoryDataStore()


def test_denied_consent_blocks_execution(clean_store: InMemoryDataStore):
    """Test 1: Actions without consent or with scope='ask_every_time' are suggested only, never executed."""
    engine = PolicyEngine(allowed_devices={"dev_firetv_living_room"})
    # Grant with scope 'ask_every_time'
    engine.set_grant(
        ConsentGrant(
            tenant_id="tenant_01",
            user_id="usr_alice",
            action_type="request_pause",
            scope="ask_every_time",
            target_device_id="dev_firetv_living_room",
        )
    )

    decision = engine.evaluate_and_execute(
        user_id="usr_alice",
        proposal_action="request_pause",
        target_device_id="dev_firetv_living_room",
        reason="Detected distraction",
        idempotency_key="idemp_alice_01",
    )

    assert decision["executed"] is False
    assert decision["decision"] == "ask_user_confirmation"
    assert "undo_token" not in decision or decision.get("undo_token") is None


def test_revoked_consent_blocks_execution(clean_store: InMemoryDataStore):
    """Test 2: Revoking an active grant prevents subsequent auto-execution."""
    engine = PolicyEngine(allowed_devices={"dev_firetv_living_room"})
    grant = ConsentGrant(
        tenant_id="tenant_01",
        user_id="usr_bob",
        action_type="request_pause",
        scope="always",
        target_device_id="dev_firetv_living_room",
    )
    engine.set_grant(grant)
    assert grant.is_valid() is True

    # Revoke grant
    revoked = engine.revoke_grant("usr_bob", "request_pause")
    assert revoked is True
    assert grant.is_valid() is False

    decision = engine.evaluate_and_execute(
        user_id="usr_bob",
        proposal_action="request_pause",
        target_device_id="dev_firetv_living_room",
        reason="Session pause",
        idempotency_key="idemp_bob_01",
    )
    assert decision["executed"] is False
    assert decision["decision"] == "blocked"


def test_duplicate_undo_requests_handled_idempotently(clean_store: InMemoryDataStore):
    """Test 3: Repeated undo calls for the same token return idempotent success without duplicate execution."""
    clean_store.audit_records.append({
        "action_id": "act_undo_test_01",
        "user_id": "usr_charlie",
        "tenant_id": "tenant_default",
        "action_type": "request_pause",
        "target_device": "dev_firetv_living_room",
        "status": "executed",
        "undo_token": "token_charlie_undo_999",
        "timestamp": time.time(),
    })

    body = {"user_id": "usr_charlie", "tenant_id": "tenant_default", "undo_token": "token_charlie_undo_999"}

    # First call reverses action
    res1 = handle_undo_action(body, store=clean_store)
    assert res1["statusCode"] == 200
    data1 = json.loads(res1["body"])
    assert data1["success"] is True

    # Second call is idempotent
    res2 = handle_undo_action(body, store=clean_store)
    assert res2["statusCode"] == 200
    data2 = json.loads(res2["body"])
    assert data2["success"] is True
    assert data2.get("idempotent") is True
    assert "already reversed" in data2["message"]



def test_invalid_undo_token_returns_honest_failure(clean_store: InMemoryDataStore):
    """Test 4: Non-existent or invalid undo token returns 404 and does not report success."""
    body = {"user_id": "usr_dave", "undo_token": "token_completely_fabricated_99999"}
    res = handle_undo_action(body, store=clean_store)
    assert res["statusCode"] == 404
    data = json.loads(res["body"])
    assert data["success"] is False
    assert data["error"] == "undo_failed"


def test_expired_undo_token_rejected_with_410(clean_store: InMemoryDataStore):
    """Test 5: Expired undo tokens exceeding TTL window are rejected with 410."""
    past_timestamp = time.time() - 400.0  # 400s in the past
    clean_store.audit_records.append({
        "action_id": "act_expired_01",
        "user_id": "usr_elena",
        "tenant_id": "tenant_default",
        "action_type": "request_pause",
        "status": "executed",
        "undo_token": "token_expired_elena",
        "timestamp": past_timestamp,
        "ttl_seconds": 300,  # 5-minute undo window
    })

    body = {"user_id": "usr_elena", "tenant_id": "tenant_default", "undo_token": "token_expired_elena"}
    res = handle_undo_action(body, store=clean_store)
    assert res["statusCode"] == 410
    data = json.loads(res["body"])
    assert data["error"] == "undo_token_expired"



def test_cross_user_undo_attempt_forbidden(clean_store: InMemoryDataStore):
    """Test 6: Attempting to reverse another user's undo token is blocked with 403 Forbidden."""
    clean_store.audit_records.append({
        "action_id": "act_victim_01",
        "user_id": "usr_victim",
        "tenant_id": "tenant_default",
        "action_type": "request_pause",
        "status": "executed",
        "undo_token": "token_victim_private_token",
        "timestamp": time.time(),
    })

    # Attacker tries to reverse victim's token
    attacker_body = {"user_id": "usr_attacker", "tenant_id": "tenant_default", "undo_token": "token_victim_private_token"}
    res = handle_undo_action(attacker_body, store=clean_store)
    assert res["statusCode"] == 403
    data = json.loads(res["body"])
    assert data["error"] == "unauthorized"
    assert "belongs to another user" in data["message"]



def test_underlying_device_reversal_failure_reported_honestly(clean_store: InMemoryDataStore):
    """Test 7: Reversal failure on an unregistered device is reported with accurate status."""
    # Register an invalid token not in device_adapter and not in audit_records
    body = {"user_id": "usr_frank", "undo_token": "undo_device_unregistered_xyz"}
    res = handle_undo_action(body, store=clean_store)
    assert res["statusCode"] == 404
    data = json.loads(res["body"])
    assert data["success"] is False
    assert "Invalid or unknown undo token" in data["message"]
