"""Unit tests verifying Durable Control-Plane Persistence, Cold-Start Recovery, and User Isolation.

Verifies:
1. Records written by one DynamoDB repository instance are readable by a second instance (cold start recovery).
2. Missing records return empty lists or None without errors.
3. Storage failures raise StorageError and translate to HTTP 503 without silently falling back to empty in-memory state.
4. User data is strictly partitioned and isolated by user_id/tenant_id.
5. In production mode (AURA_STORAGE_MODE=dynamodb or AWS environment), missing storage raises StorageUnavailableError.
"""
from __future__ import annotations

import importlib
import json
import time
from typing import Any
import pytest

cp = importlib.import_module("lambda.control_plane")
DynamoDBDataStore = cp.DynamoDBDataStore
StorageError = cp.StorageError
StorageUnavailableError = cp.StorageUnavailableError
get_datastore = cp.get_datastore
handle_get_grants = cp.handle_get_grants
handle_create_grant = cp.handle_create_grant
handle_focus_session = cp.handle_focus_session
handle_query_audit = cp.handle_query_audit
lambda_handler = cp.lambda_handler


class MockDynamoTable:
    """In-memory DynamoDB table mock with conditional operations, query, and get/put semantics."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.should_fail: bool = False

    def put_item(self, Item: dict[str, Any], ConditionExpression: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if self.should_fail:
            raise RuntimeError("Simulated DynamoDB connection drop / throughput exceeded")
        key = (Item["PK"], Item["SK"])
        if ConditionExpression:
            existing = self.items.get(key)
            if existing is not None:
                if ConditionExpression == "attribute_not_exists(PK)":
                    raise RuntimeError("ConditionalCheckFailedException: Item already exists")
                vals = kwargs.get("ExpressionAttributeValues", {})
                if ":uid" in vals and existing.get("userId") != vals[":uid"]:
                    raise RuntimeError("ConditionalCheckFailedException: User mismatch")
                if ":tid" in vals and existing.get("tenantId") != vals[":tid"]:
                    raise RuntimeError("ConditionalCheckFailedException: Tenant mismatch")
                status = existing.get("status")
                lease_exp = existing.get("leaseExpiresAt", 0)
                now = vals.get(":now", time.time())
                allowed = False
                if ":pending" in vals and status == vals[":pending"]:
                    allowed = True
                elif ":failed" in vals and status == vals[":failed"]:
                    allowed = True
                elif ":in_progress" in vals and status == vals[":in_progress"] and lease_exp < now:
                    allowed = True
                if not allowed:
                    raise RuntimeError("ConditionalCheckFailedException: Item already exists and is locked")
        self.items[key] = dict(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_item(self, Key: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if self.should_fail:
            raise RuntimeError("Simulated DynamoDB unavailable")
        key = (Key["PK"], Key["SK"])
        item = self.items.get(key)
        return {"Item": dict(item)} if item else {}

    def query(
        self,
        KeyConditionExpression: str,
        ExpressionAttributeValues: dict[str, Any],
        ScanIndexForward: bool = True,
        Limit: int = 100,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.should_fail:
            raise RuntimeError("Simulated DynamoDB query error")
        pk_val = ExpressionAttributeValues.get(":pk")
        sk_prefix = ExpressionAttributeValues.get(":sk_prefix")
        matched = []
        for (pk, sk), item in self.items.items():
            if pk == pk_val and (not sk_prefix or sk.startswith(sk_prefix)):
                matched.append(dict(item))
        matched.sort(key=lambda x: x.get("timestamp", x.get("created_at", 0)), reverse=not ScanIndexForward)
        return {"Items": matched[:Limit]}

    def update_item(
        self,
        Key: dict[str, Any],
        UpdateExpression: str,
        ConditionExpression: str | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.should_fail:
            raise RuntimeError("Simulated DynamoDB update failure")
        key = (Key["PK"], Key["SK"])
        item = self.items.get(key)
        if not item:
            item = {"PK": Key["PK"], "SK": Key["SK"]}
            self.items[key] = item

        vals = ExpressionAttributeValues or {}
        if ConditionExpression and "#status = :in_progress AND leaseOwnerId = :owner" in ConditionExpression:
            if item.get("status") != "IN_PROGRESS" or item.get("leaseOwnerId") != vals.get(":owner"):
                raise RuntimeError("ConditionalCheckFailedException: lease owner mismatch or not in progress")

        if "SET #status = :succeeded" in UpdateExpression:
            item["status"] = "SUCCEEDED"
            item["completedAt"] = vals.get(":now", int(time.time()))
        elif "SET #status = :failed" in UpdateExpression:
            item["status"] = "FAILED"
            item["failedReason"] = vals.get(":reason", "failed")
        elif "SET #active = :false" in UpdateExpression:
            item["active"] = False

        return {"Attributes": item}


@pytest.fixture
def mock_table() -> MockDynamoTable:
    return MockDynamoTable()


def test_cold_start_recovery_across_instances(mock_table: MockDynamoTable):
    """Data written by Lambda Container 1 is durable and immediately visible to Container 2."""
    container_1_store = DynamoDBDataStore(table=mock_table)
    container_2_store = DynamoDBDataStore(table=mock_table)

    # Container 1 creates grant and audit record
    user_id = "usr_marcus_coldstart"
    grant_data = {
        "grant_id": "grant_tv_pause_01",
        "user_id": user_id,
        "action_type": "request_pause",
        "scope": "allow_always",
        "active": True,
    }
    container_1_store.save_grant(grant_data)
    container_1_store.record_audit({
        "action_id": "act_cold_01",
        "user_id": user_id,
        "action_type": "request_pause",
        "status": "executed",
        "timestamp": time.time(),
    })

    # Container 2 (representing a cold start) reads the records
    grants = container_2_store.get_grants(user_id)
    assert len(grants) == 1
    assert grants[0]["grant_id"] == "grant_tv_pause_01"
    assert grants[0]["action_type"] == "request_pause"

    audits = container_2_store.get_audit_records(user_id)
    assert len(audits) == 1
    assert audits[0]["action_id"] == "act_cold_01"


def test_missing_records_return_empty_safely(mock_table: MockDynamoTable):
    """Querying an unknown user returns empty lists, not errors."""
    store = DynamoDBDataStore(table=mock_table)
    assert store.get_grants("usr_nonexistent") == []
    assert store.get_audit_records("usr_nonexistent") == []
    assert store.get_undo_record("token_nonexistent") is None
    assert store.get_focus_session("usr_nonexistent", "sess_none") is None


def test_dynamodb_failure_raises_storage_error_and_returns_503(mock_table: MockDynamoTable):
    """Storage failure raises StorageError and returns 503 instead of empty in-memory fallback."""
    store = DynamoDBDataStore(table=mock_table)
    mock_table.should_fail = True

    # Method call raises StorageError
    with pytest.raises(StorageError):
        store.get_grants("usr_marcus")

    # API handler returns HTTP 503
    resp = handle_get_grants("usr_marcus", store=store)
    assert resp["statusCode"] == 503
    body = json.loads(resp["body"])
    assert body["error"] == "storage_error"


def test_user_data_isolation_in_durable_store(mock_table: MockDynamoTable):
    """User A cannot observe or query User B's durable records."""
    store = DynamoDBDataStore(table=mock_table)

    store.save_grant({"grant_id": "grant_a", "user_id": "usr_alice", "action_type": "nudge", "scope": "allow_always"})
    store.save_grant({"grant_id": "grant_b", "user_id": "usr_bob", "action_type": "request_dnd", "scope": "allow_always"})

    alice_grants = store.get_grants("usr_alice")
    bob_grants = store.get_grants("usr_bob")

    assert len(alice_grants) == 1
    assert alice_grants[0]["grant_id"] == "grant_a"

    assert len(bob_grants) == 1
    assert bob_grants[0]["grant_id"] == "grant_b"


def test_production_mode_requires_dynamodb_and_fails_safely_when_unavailable(monkeypatch):
    """In production mode, missing storage raises StorageUnavailableError and never silently uses in-memory."""
    monkeypatch.setenv("AURA_STORAGE_MODE", "dynamodb")
    monkeypatch.setenv("AURA_CORE_TABLE", "NonExistentTable123")

    # Without store_override, get_datastore attempts to initialize DynamoDB and raises
    with pytest.raises(StorageUnavailableError):
        get_datastore(store_override=None)

    # In lambda_handler, returns HTTP 503
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"user_id": "usr_test"},
    }
    resp = lambda_handler(event, store_override=None)
    assert resp["statusCode"] == 503
    body = json.loads(resp["body"])
    assert body["error"] == "storage_unavailable"
