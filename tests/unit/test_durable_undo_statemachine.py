"""Unit tests for Durable Undo State Machine, Concurrency Locking, and Idempotency.

Verifies:
- State transitions: PENDING -> IN_PROGRESS -> SUCCEEDED / FAILED.
- Repeated undo calls on SUCCEEDED are idempotent (HTTP 200, idempotent: True).
- Concurrent undo calls during active IN_PROGRESS lease are locked (HTTP 409 Conflict).
- Expired processing leases allow clean recovery and completion.
- Transient device failures transition to FAILED and return HTTP 502 (retryable: True)
  without permanently consuming the token.
- Cross-user undo attempts are blocked with HTTP 403 Forbidden.
- Expired tokens past TTL window are rejected with HTTP 410 Gone.
"""
from __future__ import annotations

import importlib
import json
import time
from typing import Any
import pytest

cp = importlib.import_module("lambda.control_plane")
InMemoryDataStore = cp.InMemoryDataStore
handle_undo_action = cp.handle_undo_action


@pytest.fixture
def store() -> InMemoryDataStore:
    return InMemoryDataStore()


# ---------------------------------------------------------------------------
# Test 1: Full successful lifecycle: PENDING -> IN_PROGRESS -> SUCCEEDED
# ---------------------------------------------------------------------------

def test_undo_state_transition_lifecycle(store: InMemoryDataStore):
    user_id = "usr_marcus_undo"
    token = "token_state_test_001"
    now = time.time()

    store.undo_records[token] = {
        "token": token,
        "user_id": user_id,
        "userId": user_id,
        "tenant_id": "tenant_default",
        "tenantId": "tenant_default",
        "action_id": "act_001",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
        "lease_expires_at": 0.0,
        "retry_count": 0,
    }
    store.audit_records.append({
        "action_id": "act_001",
        "user_id": user_id,
        "tenant_id": "tenant_default",
        "action_type": "request_pause",
        "target_device": "dev_firetv_living_room",
        "status": "executed",
        "undo_token": token,
        "timestamp": now,
    })

    # Execute undo
    res = handle_undo_action({"user_id": user_id, "tenant_id": "tenant_default", "undo_token": token}, store=store)
    assert res["statusCode"] == 200
    data = json.loads(res["body"])
    assert data["success"] is True

    # State must be terminal SUCCEEDED
    assert store.undo_records[token]["status"] == "SUCCEEDED"
    assert token in store.undone_tokens


# ---------------------------------------------------------------------------
# Test 2: Idempotent replay on SUCCEEDED returns HTTP 200
# ---------------------------------------------------------------------------

def test_idempotent_replay_on_succeeded(store: InMemoryDataStore):
    user_id = "usr_marcus_undo"
    token = "token_succeeded_002"
    now = time.time()

    store.undo_records[token] = {
        "token": token,
        "user_id": user_id,
        "userId": user_id,
        "tenant_id": "tenant_default",
        "tenantId": "tenant_default",
        "status": "SUCCEEDED",
        "created_at": now - 100,
        "expires_at": now + 500,
    }
    store.undone_tokens.add(token)

    res = handle_undo_action({"user_id": user_id, "tenant_id": "tenant_default", "undo_token": token}, store=store)
    assert res["statusCode"] == 200
    data = json.loads(res["body"])
    assert data["success"] is True
    assert data.get("idempotent") is True
    assert "already reversed" in data["message"]


# ---------------------------------------------------------------------------
# Test 3: Concurrency locking: IN_PROGRESS lease returns HTTP 409 Conflict
# ---------------------------------------------------------------------------

def test_concurrent_undo_in_progress_returns_409(store: InMemoryDataStore):
    user_id = "usr_marcus_undo"
    token = "token_locked_003"
    now = time.time()

    # Active lease valid for another 20 seconds
    store.undo_records[token] = {
        "token": token,
        "user_id": user_id,
        "userId": user_id,
        "tenant_id": "tenant_default",
        "tenantId": "tenant_default",
        "status": "IN_PROGRESS",
        "created_at": now - 10,
        "lease_expires_at": now + 20.0,
        "leaseExpiresAt": int(now + 20),
        "expires_at": now + 600,
    }

    res = handle_undo_action({"user_id": user_id, "tenant_id": "tenant_default", "undo_token": token}, store=store)
    assert res["statusCode"] == 409
    data = json.loads(res["body"])
    assert data["error"] == "undo_in_progress"
    assert "currently in progress" in data["message"]


# ---------------------------------------------------------------------------
# Test 4: Expired processing lease allows recovery and completion
# ---------------------------------------------------------------------------

def test_expired_lease_reacquired_and_completed(store: InMemoryDataStore):
    user_id = "usr_marcus_undo"
    token = "token_expired_lease_004"
    now = time.time()

    # Worker crashed 40 seconds ago; lease expired (lease_expires_at was 10s ago)
    store.undo_records[token] = {
        "token": token,
        "user_id": user_id,
        "userId": user_id,
        "tenant_id": "tenant_default",
        "tenantId": "tenant_default",
        "status": "IN_PROGRESS",
        "created_at": now - 60,
        "lease_expires_at": now - 10.0,  # Expired lease
        "leaseExpiresAt": int(now - 10),
        "expires_at": now + 600,
    }
    store.audit_records.append({
        "action_id": "act_lease_004",
        "user_id": user_id,
        "tenant_id": "tenant_default",
        "action_type": "request_pause",
        "status": "executed",
        "undo_token": token,
        "timestamp": now - 60,
    })

    # Next attempt acquires lease and succeeds
    res = handle_undo_action({"user_id": user_id, "tenant_id": "tenant_default", "undo_token": token}, store=store)
    assert res["statusCode"] == 200
    assert store.undo_records[token]["status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Test 5: Device offline failure transitions to FAILED without consuming token
# ---------------------------------------------------------------------------

def test_device_offline_failure_reported_honestly_as_retryable(store: InMemoryDataStore):
    user_id = "usr_marcus_undo"
    token = "token_device_offline_005"
    now = time.time()

    store.undo_records[token] = {
        "token": token,
        "user_id": user_id,
        "userId": user_id,
        "tenant_id": "tenant_default",
        "tenantId": "tenant_default",
        "action_id": "act_005",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    }
    store.audit_records.append({
        "action_id": "act_005",
        "user_id": user_id,
        "tenant_id": "tenant_default",
        "action_type": "request_pause",
        "status": "executed",
        "undo_token": token,
        "timestamp": now,
    })

    # Simulate device adapter reporting offline failure
    original_exec = store.device_adapter.execute_undo
    store.device_adapter.execute_undo = lambda tok: {"success": False, "error": "device_offline"}

    try:
        res = handle_undo_action({"user_id": user_id, "tenant_id": "tenant_default", "undo_token": token}, store=store)
        assert res["statusCode"] == 502
        data = json.loads(res["body"])
        assert data["success"] is False
        assert data["retryable"] is True
        assert data["error"] == "device_reversal_failed"

        # Token was NOT marked SUCCEEDED
        assert store.undo_records[token]["status"] == "FAILED"
        assert token not in store.undone_tokens
    finally:
        store.device_adapter.execute_undo = original_exec


# ---------------------------------------------------------------------------
# Test 6: Cross-user undo attempt blocked with HTTP 403 Forbidden
# ---------------------------------------------------------------------------

def test_cross_user_undo_attempt_blocked(store: InMemoryDataStore):
    token = "token_private_elena_006"
    now = time.time()

    store.undo_records[token] = {
        "token": token,
        "user_id": "usr_elena",
        "userId": "usr_elena",
        "tenant_id": "tenant_default",
        "tenantId": "tenant_default",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    }

    attacker_res = handle_undo_action({"user_id": "usr_attacker", "tenant_id": "tenant_default", "undo_token": token}, store=store)
    assert attacker_res["statusCode"] == 403
    data = json.loads(attacker_res["body"])
    assert data["error"] == "unauthorized"
    assert "belongs to another user" in data["message"]


# ---------------------------------------------------------------------------
# Test 7: Expired undo token past TTL window returns HTTP 410 Gone
# ---------------------------------------------------------------------------

def test_expired_undo_token_returns_410(store: InMemoryDataStore):
    token = "token_ttl_expired_007"
    past = time.time() - 400.0

    store.undo_records[token] = {
        "token": token,
        "user_id": "usr_marcus",
        "userId": "usr_marcus",
        "tenant_id": "tenant_default",
        "tenantId": "tenant_default",
        "status": "PENDING",
        "created_at": past,
        "expires_at": past + 300.0,  # Expired 100 seconds ago
    }

    res = handle_undo_action({"user_id": "usr_marcus", "tenant_id": "tenant_default", "undo_token": token}, store=store)
    assert res["statusCode"] == 410
    data = json.loads(res["body"])
    assert data["error"] == "undo_token_expired"


# ---------------------------------------------------------------------------
# Test 8: Stale-worker completion rejected after lease reacquired by newer worker
# ---------------------------------------------------------------------------

def test_stale_worker_completion_rejected(store: InMemoryDataStore):
    """An expired worker with an old fencing token cannot mark state SUCCEEDED."""
    token = "token_stale_worker_008"
    user_id = "usr_marcus"

    # Worker 1 acquires lease
    acquired_1, _, _ = store.acquire_undo_lease(token, user_id, "worker_1_id", lease_duration=5.0)
    assert acquired_1 is True

    # Worker 1 lease expires (fast-forward time in record)
    store.undo_records[token]["lease_expires_at"] = time.time() - 1.0

    # Worker 2 reacquires lease
    acquired_2, _, _ = store.acquire_undo_lease(token, user_id, "worker_2_id", lease_duration=30.0)
    assert acquired_2 is True

    # Stale Worker 1 wakes up and attempts to complete undo
    completed_stale = store.complete_undo(token, user_id, "worker_1_id")
    assert completed_stale is False  # Fencing token mismatch rejects stale worker

    # Worker 2 completes undo legitimately
    completed_legit = store.complete_undo(token, user_id, "worker_2_id")
    assert completed_legit is True
    assert store.undo_records[token]["status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Test 9: Concurrent lease acquisition cannot succeed twice
# ---------------------------------------------------------------------------

def test_concurrent_lease_acquisition_cannot_succeed_twice(store: InMemoryDataStore):
    """Two concurrent workers competing for the same undo token: exactly one gets lease."""
    token = "token_race_009"
    user_id = "usr_marcus"

    acquired_worker_a, status_a, _ = store.acquire_undo_lease(token, user_id, "worker_a", lease_duration=30.0)
    acquired_worker_b, status_b, _ = store.acquire_undo_lease(token, user_id, "worker_b", lease_duration=30.0)

    assert acquired_worker_a is True
    assert status_a == 200

    assert acquired_worker_b is False
    assert status_b == 409  # Locked by worker a

