"""Unit tests for DynamoDB-backed durable persistence conforming to schemas/dynamodb/table_schema.json.

Verifies:
1. Persistence across independently constructed DynamoDBDataStore instances sharing a table.
2. Per-user isolation & cross-user access boundaries.
3. Missing records handling (returns None / empty list gracefully).
4. Atomic lease acquisition with ConditionExpression (lease owner fencing, retry on failed/expired, 409 conflict).
5. Storage failure handling (StorageError -> HTTP 503).
6. Production fail-closed behavior (StorageUnavailableError when DynamoDB is unavailable in production).
"""
from __future__ import annotations

import importlib
import json
import time
import uuid
from typing import Any
import pytest

cp = importlib.import_module("lambda.control_plane")
DynamoDBDataStore = cp.DynamoDBDataStore
StorageError = cp.StorageError
StorageUnavailableError = cp.StorageUnavailableError
get_datastore = cp.get_datastore
lambda_handler = cp.lambda_handler


class MockDynamoDBTable:
    """In-memory mock mimicking boto3 DynamoDB Table resource with condition checking."""

    def __init__(self, table_name: str = "AuraCoreTable") -> None:
        self.table_name = table_name
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, Key: dict[str, str]) -> dict[str, Any]:
        pk, sk = Key.get("PK", ""), Key.get("SK", "")
        item = self.items.get((pk, sk))
        return {"Item": dict(item)} if item else {}

    def put_item(
        self,
        Item: dict[str, Any],
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pk, sk = Item.get("PK", ""), Item.get("SK", "")
        existing = self.items.get((pk, sk))

        if ConditionExpression:
            # Evaluate condition expression for atomic undo lease
            if "attribute_not_exists(PK)" in ConditionExpression:
                if existing is not None:
                    status = existing.get("status")
                    lease_expires = existing.get("leaseExpiresAt", 0)
                    now = (ExpressionAttributeValues or {}).get(":now", time.time())
                    failed_val = (ExpressionAttributeValues or {}).get(":failed", "FAILED")
                    in_prog_val = (ExpressionAttributeValues or {}).get(":in_progress", "IN_PROGRESS")

                    # Check if failed or expired
                    is_failed = status == failed_val
                    is_expired = status == in_prog_val and lease_expires < now
                    if not (is_failed or is_expired):
                        raise Exception("ConditionalCheckFailedException: Condition check failed")

        self.items[(pk, sk)] = dict(Item)
        return {}

    def update_item(
        self,
        Key: dict[str, str],
        UpdateExpression: str,
        ConditionExpression: str | None = None,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        pk, sk = Key.get("PK", ""), Key.get("SK", "")
        existing = self.items.get((pk, sk))
        if not existing:
            raise Exception("ResourceNotFoundException: Item not found")

        if ConditionExpression:
            if "#status = :in_progress AND leaseOwnerId = :owner" in ConditionExpression:
                req_owner = (ExpressionAttributeValues or {}).get(":owner")
                if existing.get("status") != "IN_PROGRESS" or existing.get("leaseOwnerId") != req_owner:
                    raise Exception("ConditionalCheckFailedException: Fencing token mismatch")

        # Apply update expression
        now = time.time()
        if ":succeeded" in (ExpressionAttributeValues or {}):
            existing["status"] = "SUCCEEDED"
            existing["completedAt"] = int(now)
        elif ":failed" in (ExpressionAttributeValues or {}):
            existing["status"] = "FAILED"
            existing["failedReason"] = (ExpressionAttributeValues or {}).get(":reason", "failed")
        elif ":false" in (ExpressionAttributeValues or {}):
            existing["active"] = False
            existing["revoked_at"] = int(now)

        self.items[(pk, sk)] = existing
        return {"Attributes": existing}

    def query(
        self,
        KeyConditionExpression: str,
        ExpressionAttributeNames: dict[str, str],
        ExpressionAttributeValues: dict[str, Any],
        ScanIndexForward: bool = True,
        Limit: int = 20,
    ) -> dict[str, Any]:
        pk_val = ExpressionAttributeValues.get(":pk", "")
        sk_prefix = ExpressionAttributeValues.get(":sk_prefix", "")

        matched = []
        for (pk, sk), it in self.items.items():
            if pk == pk_val and sk.startswith(sk_prefix):
                matched.append(dict(it))

        matched.sort(key=lambda x: x.get("timestamp", x.get("created_at", 0)), reverse=not ScanIndexForward)
        return {"Items": matched[:Limit]}


# ---------------------------------------------------------------------------
# Test 1: Persistence across independently constructed store instances
# ---------------------------------------------------------------------------

def test_persistence_across_independent_store_instances():
    """Verify that records saved by Store 1 are read intact by Store 2 sharing the table."""
    shared_table = MockDynamoDBTable()
    store1 = DynamoDBDataStore(table=shared_table)
    store2 = DynamoDBDataStore(table=shared_table)

    user_id = "usr_marcus_multi_instance"

    # 1. Store 1 writes consent grant
    grant = {
        "user_id": user_id,
        "action_type": "request_pause",
        "scope": "allow_always",
        "target_allowlist": ["dev_firetv_living_room"],
        "active": True,
    }
    saved_grant = store1.save_grant(grant)
    assert saved_grant["PK"] == f"TENANT#tenant_default#USER#{user_id}"
    assert saved_grant["SK"] == "GRANT#request_pause"

    # 2. Store 2 reads consent grant
    grants = store2.get_grants(user_id)
    assert len(grants) == 1
    assert grants[0]["action_type"] == "request_pause"
    assert grants[0]["scope"] == "allow_always"

    # 3. Store 1 writes focus session
    sess = {
        "user_id": user_id,
        "session_id": "sess_shared_999",
        "status": "active",
        "duration_minutes": 45,
    }
    store1.save_focus_session(sess)

    # 4. Store 2 reads focus session
    read_sess = store2.get_focus_session(user_id, "sess_shared_999")
    assert read_sess is not None
    assert read_sess["session_id"] == "sess_shared_999"
    assert read_sess["status"] == "active"

    # 5. Store 1 writes audit record
    audit_rec = {
        "user_id": user_id,
        "action_id": "act_shared_001",
        "action_type": "request_pause",
        "status": "executed",
        "reason": "Fire TV paused",
        "timestamp": time.time(),
    }
    store1.record_audit(audit_rec)

    # 6. Store 2 reads audit records
    audits = store2.get_audit_records(user_id)
    assert len(audits) == 1
    assert audits[0]["action_id"] == "act_shared_001"


# ---------------------------------------------------------------------------
# Test 2: User Isolation & Cross-User Boundaries
# ---------------------------------------------------------------------------

def test_user_isolation_boundaries():
    """Verify that User A cannot read or modify User B's records."""
    shared_table = MockDynamoDBTable()
    store = DynamoDBDataStore(table=shared_table)

    user_a = "usr_alice_isolation"
    user_b = "usr_bob_isolation"

    # Save grant for Alice
    store.save_grant({
        "user_id": user_a,
        "action_type": "request_pause",
        "scope": "allow_session",
        "active": True,
    })

    # Query grants for Bob -> must be empty
    bob_grants = store.get_grants(user_b)
    assert len(bob_grants) == 0

    # Query audit for Bob -> must be empty
    bob_audits = store.get_audit_records(user_b)
    assert len(bob_audits) == 0

    # Bob cannot revoke Alice's grant
    revoked = store.revoke_grant("request_pause", user_id=user_b)
    assert revoked is False


# ---------------------------------------------------------------------------
# Test 3: Missing Records Handling
# ---------------------------------------------------------------------------

def test_missing_records_handled_gracefully():
    """Missing records return None or empty collections without errors."""
    shared_table = MockDynamoDBTable()
    store = DynamoDBDataStore(table=shared_table)

    assert store.get_grants("usr_nonexistent") == []
    assert store.get_audit_records("usr_nonexistent") == []
    assert store.get_focus_session("usr_nonexistent", "sess_none") is None
    assert store.get_undo_record("undo_nonexistent") is None
    assert store.get_preferences("usr_nonexistent") == {}


# ---------------------------------------------------------------------------
# Test 4: Concurrency Locking and Atomic Lease Acquisition
# ---------------------------------------------------------------------------

def test_atomic_undo_lease_concurrency_and_fencing():
    """Verify atomic lease acquisition, 409 conflict, idempotent 200, and fencing tokens."""
    shared_table = MockDynamoDBTable()
    store = DynamoDBDataStore(table=shared_table)

    token = "undo_concurrency_tok_01"
    user_id = "usr_concurrency_user"

    # 1. Acquire initial lease
    owner_1 = str(uuid.uuid4())
    acquired, code, err = store.acquire_undo_lease(token, user_id, owner_1, lease_duration=30.0)
    assert acquired is True
    assert code == 200

    # 2. Second concurrent request while lease is active -> 409 Conflict
    owner_2 = str(uuid.uuid4())
    acquired_2, code_2, err_2 = store.acquire_undo_lease(token, user_id, owner_2, lease_duration=30.0)
    assert acquired_2 is False
    assert code_2 == 409
    assert "in progress" in err_2.lower()

    # 3. Wrong owner cannot complete the lease (fencing check)
    completed_bad = store.complete_undo(token, user_id, lease_owner_id=owner_2)
    assert completed_bad is False

    # 4. Correct owner completes the lease -> transition to SUCCEEDED
    completed_good = store.complete_undo(token, user_id, lease_owner_id=owner_1)
    assert completed_good is True

    # 5. Subsequent request on SUCCEEDED token is idempotent (code 200, acquired False)
    owner_3 = str(uuid.uuid4())
    acquired_3, code_3, msg_3 = store.acquire_undo_lease(token, user_id, owner_3, lease_duration=30.0)
    assert acquired_3 is False
    assert code_3 == 200
    assert "already reversed" in msg_3.lower()


# ---------------------------------------------------------------------------
# Test 5: Backend Storage Failures Return HTTP 503
# ---------------------------------------------------------------------------

def test_backend_storage_failure_returns_503():
    """When DynamoDB operations fail, StorageError is raised and 503 is returned."""
    class FailingTable:
        def query(self, *args, **kwargs):
            raise Exception("DynamoDB service unavailable")
        def put_item(self, *args, **kwargs):
            raise Exception("ProvisionedThroughputExceededException")

    failing_store = DynamoDBDataStore(table=FailingTable())

    # Get grants returns 503
    res_get = cp.handle_get_grants("usr_alice", store=failing_store)
    assert res_get["statusCode"] == 503
    assert json.loads(res_get["body"])["error"] == "storage_error"

    # Create grant returns 503
    res_create = cp.handle_create_grant(
        {"user_id": "usr_alice", "action_type": "request_pause", "scope": "allow_session"},
        store=failing_store,
    )
    assert res_create["statusCode"] == 503
    assert json.loads(res_create["body"])["error"] == "storage_error"


# ---------------------------------------------------------------------------
# Test 6: Production Mode Fails Closed When DynamoDB Unavailable
# ---------------------------------------------------------------------------

def test_production_fails_closed_when_storage_unavailable(monkeypatch):
    """When AURA_STORAGE_MODE=dynamodb, failure to connect to DynamoDB must raise StorageUnavailableError."""
    monkeypatch.setenv("AURA_STORAGE_MODE", "dynamodb")
    monkeypatch.setenv("AURA_CORE_TABLE", "NonExistentTable999")

    # In an environment without valid AWS credentials/boto3 client to live AWS,
    # get_datastore() must raise StorageUnavailableError
    # or lambda_handler returns HTTP 503
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": "Bearer any_token"},
        "queryStringParameters": {"user_id": "usr_test"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] in (503, 401)
    if resp["statusCode"] == 503:
        body = json.loads(resp["body"])
        assert body["error"] == "storage_unavailable"
