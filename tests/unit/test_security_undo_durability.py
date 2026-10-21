from __future__ import annotations

import importlib
import json
import time
from typing import Any
import pytest

cp = importlib.import_module("lambda.control_plane")
auth_mod = importlib.import_module("lambda.auth")
InMemoryDataStore = cp.InMemoryDataStore
DynamoDBDataStore = cp.DynamoDBDataStore
StorageError = cp.StorageError
handle_undo_action = cp.handle_undo_action
handle_focus_signal = cp.handle_focus_signal
handle_publish_slack_status = cp.handle_publish_slack_status
lambda_handler = cp.lambda_handler


class RealisticMockDynamoTable:
    """Mock DynamoDB table simulating strict PK/SK partition, conditions, and lease fencing."""

    def __init__(self, table_name: str = "AuraCoreTable") -> None:
        self.table_name = table_name
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.should_fail: bool = False

    def get_item(self, Key: dict[str, str]) -> dict[str, Any]:
        if self.should_fail:
            raise RuntimeError("DynamoDB service unavailable")
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
        if self.should_fail:
            raise RuntimeError("DynamoDB service unavailable")
        pk, sk = Item.get("PK", ""), Item.get("SK", "")
        existing = self.items.get((pk, sk))

        if ConditionExpression:
            if existing is not None:
                if ConditionExpression == "attribute_not_exists(PK)":
                    raise Exception("ConditionalCheckFailedException: Item already exists")
                vals = ExpressionAttributeValues or {}
                if ":uid" in vals and existing.get("userId") != vals[":uid"]:
                    raise Exception("ConditionalCheckFailedException: User mismatch")
                if ":tid" in vals and existing.get("tenantId") != vals[":tid"]:
                    raise Exception("ConditionalCheckFailedException: Tenant mismatch")
                status = existing.get("status")
                lease_expires = existing.get("leaseExpiresAt", 0)
                now = vals.get(":now", time.time())
                pending_val = vals.get(":pending", "PENDING")
                failed_val = vals.get(":failed", "FAILED")
                in_prog_val = vals.get(":in_progress", "IN_PROGRESS")

                is_pending = status == pending_val
                is_failed = status == failed_val
                is_expired = status == in_prog_val and lease_expires < now
                if not (is_pending or is_failed or is_expired):
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
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.should_fail:
            raise RuntimeError("DynamoDB service unavailable")
        pk, sk = Key.get("PK", ""), Key.get("SK", "")
        existing = self.items.get((pk, sk))
        if not existing:
            raise Exception("ResourceNotFoundException: Item not found")

        if ConditionExpression:
            if "#status = :in_progress AND leaseOwnerId = :owner" in ConditionExpression:
                req_owner = (ExpressionAttributeValues or {}).get(":owner")
                if existing.get("status") != "IN_PROGRESS" or existing.get("leaseOwnerId") != req_owner:
                    raise Exception("ConditionalCheckFailedException: Fencing token mismatch")

        now = time.time()
        vals = ExpressionAttributeValues or {}
        if ":step_list" in vals:
            current_revs = list(existing.get("completed_reversals") or [])
            for s in vals[":step_list"]:
                if s not in current_revs:
                    current_revs.append(s)
            existing["completed_reversals"] = current_revs
        elif ":succeeded" in vals:
            existing["status"] = "SUCCEEDED"
            existing["completedAt"] = int(now)
            if ":completed_revs" in vals:
                existing["completed_reversals"] = list(vals[":completed_revs"])
        elif ":failed" in vals:
            existing["status"] = "FAILED"
            existing["failedReason"] = vals.get(":reason", "failed")
            if ":completed_revs" in vals:
                existing["completed_reversals"] = list(vals[":completed_revs"])

        self.items[(pk, sk)] = existing
        return {}


# ---------------------------------------------------------------------------
# Test 1: Same owner and tenant can perform a valid undo
# ---------------------------------------------------------------------------

def test_same_owner_and_tenant_can_perform_valid_undo():
    """Legitimate owner within the same tenant can reverse an action."""
    table = RealisticMockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_alice"
    tenant_id = "tenant_finance"
    token = "undo_tok_valid_001"
    now = time.time()

    store.save_undo_record({
        "token": token,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_id": "act_valid_01",
        "action_type": "request_pause",
        "target_device": "dev_firetv_01",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    })

    # Legitimate same-user, same-tenant undo
    body = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "undo_token": token,
    }
    resp = handle_undo_action(body, store=store)
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])
    assert data["success"] is True
    assert data["undo_token"] == token

    # Check terminal state in DynamoDB
    record = store.get_undo_record(token)
    assert record is not None
    assert record["status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Test 2: tenant_default cannot bypass a tenant mismatch
# ---------------------------------------------------------------------------

def test_tenant_default_cannot_bypass_tenant_mismatch():
    """tenant_default cannot undo tokens belonging to specific tenants, and vice-versa."""
    table = RealisticMockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_alice"
    token_corp = "undo_tok_corp_002"
    now = time.time()

    # Record belongs to tenant_acme
    store.save_undo_record({
        "token": token_corp,
        "userId": user_id,
        "tenantId": "tenant_acme",
        "action_id": "act_corp_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    })

    # Attacker in tenant_default attempts to undo tenant_acme's token
    attacker_body_1 = {
        "user_id": user_id,
        "tenant_id": "tenant_default",
        "undo_token": token_corp,
    }
    resp1 = handle_undo_action(attacker_body_1, store=store)
    assert resp1["statusCode"] == 403
    data1 = json.loads(resp1["body"])
    assert data1["error"] == "unauthorized"
    assert "another tenant" in data1["message"]

    # Conversely: record in tenant_default cannot be undone by tenant_acme
    token_def = "undo_tok_def_003"
    store.save_undo_record({
        "token": token_def,
        "userId": user_id,
        "tenantId": "tenant_default",
        "action_id": "act_def_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    })

    attacker_body_2 = {
        "user_id": user_id,
        "tenant_id": "tenant_acme",
        "undo_token": token_def,
    }
    resp2 = handle_undo_action(attacker_body_2, store=store)
    assert resp2["statusCode"] == 403
    data2 = json.loads(resp2["body"])
    assert data2["error"] == "unauthorized"
    assert "another tenant" in data2["message"]


# ---------------------------------------------------------------------------
# Test 3: A different user in the same tenant cannot use another user's token
# ---------------------------------------------------------------------------

def test_different_user_in_same_tenant_blocked():
    """A caller from the same tenant cannot reverse a token owned by another user."""
    table = RealisticMockDynamoTable()
    store = DynamoDBDataStore(table=table)
    tenant_id = "tenant_enterprise"
    token = "undo_tok_user_isolation_004"
    now = time.time()

    store.save_undo_record({
        "token": token,
        "userId": "usr_alice",
        "tenantId": tenant_id,
        "action_id": "act_iso_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    })

    # usr_bob in the exact same tenant tries to undo
    bob_body = {
        "user_id": "usr_bob",
        "tenant_id": tenant_id,
        "undo_token": token,
    }
    resp = handle_undo_action(bob_body, store=store)
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert data["error"] == "unauthorized"
    assert "another user" in data["message"]


# ---------------------------------------------------------------------------
# Test 4: Missing tenant or owner information fails closed
# ---------------------------------------------------------------------------

def test_missing_tenant_or_owner_information_fails_closed():
    """Requests with missing user identity or tokens with missing owner fail closed."""
    store = InMemoryDataStore()
    token = "undo_tok_missing_info_005"
    now = time.time()

    # 1. Missing user_id in request
    resp1 = handle_undo_action({"undo_token": token}, store=store)
    assert resp1["statusCode"] == 400

    # 2. Empty string user_id in request
    resp2 = handle_undo_action({"user_id": "   ", "undo_token": token}, store=store)
    assert resp2["statusCode"] == 400

    # 3. Missing undo_token in request
    resp3 = handle_undo_action({"user_id": "usr_alice"}, store=store)
    assert resp3["statusCode"] == 400

    # 4. Record in datastore with missing/empty owner user_id fails closed
    store.undo_records[token] = {
        "token": token,
        "user_id": "",
        "userId": "",
        "tenant_id": "tenant_default",
        "action_id": "act_ownerless_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    }
    resp4 = handle_undo_action({"user_id": "usr_alice", "undo_token": token}, store=store)
    assert resp4["statusCode"] == 403
    data4 = json.loads(resp4["body"])
    assert data4["error"] == "unauthorized"
    assert "no assigned owner" in data4["message"]


# ---------------------------------------------------------------------------
# Test 5: Inconsistent ownership records fail closed
# ---------------------------------------------------------------------------

def test_inconsistent_ownership_records_fail_closed():
    """Discrepancies between undo_records and audit_records trigger fail-closed rejection."""
    store = InMemoryDataStore()
    token = "undo_tok_inconsistent_006"
    now = time.time()

    # Inconsistent user ownership: undo_record says usr_alice, audit_record says usr_bob
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": "tenant_default",
        "action_id": "act_incon_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    }
    store.audit_records.append({
        "undo_token": token,
        "user_id": "usr_bob",
        "tenant_id": "tenant_default",
        "action_id": "act_incon_01",
    })

    resp = handle_undo_action({"user_id": "usr_alice", "undo_token": token}, store=store)
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert "Inconsistent user ownership" in data["message"]

    # Inconsistent tenant ownership: undo_record says tenant_alpha, audit says tenant_beta
    token_tenant = "undo_tok_tenant_inconsistent_007"
    store.undo_records[token_tenant] = {
        "token": token_tenant,
        "userId": "usr_alice",
        "tenantId": "tenant_alpha",
        "action_id": "act_incon_02",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    }
    store.audit_records.append({
        "undo_token": token_tenant,
        "user_id": "usr_alice",
        "tenant_id": "tenant_beta",
        "action_id": "act_incon_02",
    })

    resp_tenant = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_alpha", "undo_token": token_tenant},
        store=store,
    )
    assert resp_tenant["statusCode"] == 403
    data_t = json.loads(resp_tenant["body"])
    assert "Inconsistent tenant ownership" in data_t["message"]


# ---------------------------------------------------------------------------
# Test 6: Undo works after simulated process restart using only durable storage
# ---------------------------------------------------------------------------

def test_undo_works_after_simulated_process_restart():
    """Verify undo succeeds after a complete cold restart with fresh store instance."""
    table = RealisticMockDynamoTable()
    user_id = "usr_cold_start"
    tenant_id = "tenant_cold"
    token = "undo_tok_restart_008"
    now = time.time()

    # Step 1: Process A issues the undo token durably into DynamoDB
    process_a_store = DynamoDBDataStore(table=table)
    process_a_store.save_undo_record({
        "token": token,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_id": "act_restart_01",
        "action_type": "request_pause",
        "target_device": "dev_firetv_living_room",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    })

    # Process A crashes and its in-memory state is completely discarded
    del process_a_store

    # Step 2: Fresh Process B starts up with zero in-memory knowledge of the token
    process_b_store = DynamoDBDataStore(table=table)
    assert token not in process_b_store.undo_records
    assert token not in process_b_store.undone_tokens

    # Process B executes undo using durable DynamoDB storage
    resp = handle_undo_action(
        {"user_id": user_id, "tenant_id": tenant_id, "undo_token": token},
        store=process_b_store,
    )
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])
    assert data["success"] is True

    # Process B crashes and Process C starts up
    del process_b_store
    process_c_store = DynamoDBDataStore(table=table)

    # Process C verifies idempotent replay protection from durable record
    replay_resp = handle_undo_action(
        {"user_id": user_id, "tenant_id": tenant_id, "undo_token": token},
        store=process_c_store,
    )
    assert replay_resp["statusCode"] == 200
    replay_data = json.loads(replay_resp["body"])
    assert replay_data["success"] is True
    assert replay_data.get("idempotent") is True


# ---------------------------------------------------------------------------
# Test 7: Missing or expired tokens are rejected
# ---------------------------------------------------------------------------

def test_missing_or_expired_tokens_are_rejected():
    """Non-existent tokens return 404; expired tokens return 410."""
    store = InMemoryDataStore()
    user_id = "usr_alice"
    now = time.time()

    # Non-existent token -> 404
    missing_resp = handle_undo_action(
        {"user_id": user_id, "undo_token": "token_completely_fabricated"},
        store=store,
    )
    assert missing_resp["statusCode"] == 404
    missing_data = json.loads(missing_resp["body"])
    assert missing_data["error"] == "undo_failed"

    # Expired token in store.undo_records -> 410
    expired_token = "undo_tok_expired_009"
    store.undo_records[expired_token] = {
        "token": expired_token,
        "userId": user_id,
        "tenantId": "tenant_default",
        "action_id": "act_exp_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": now - 1000,
        "expires_at": now - 100,  # Expired in past
    }
    expired_resp = handle_undo_action(
        {"user_id": user_id, "undo_token": expired_token},
        store=store,
    )
    assert expired_resp["statusCode"] == 410
    expired_data = json.loads(expired_resp["body"])
    assert expired_data["error"] == "undo_token_expired"


# ---------------------------------------------------------------------------
# Test 8: Concurrent or repeated undo requests cannot execute the same action twice
# ---------------------------------------------------------------------------

def test_concurrent_or_repeated_undo_requests_cannot_execute_twice():
    """Concurrent execution is serialized; second attempt gets 409 or idempotent 200."""
    table = RealisticMockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_alice"
    tenant_id = "tenant_test"
    token = "undo_tok_concurrent_010"
    now = time.time()

    store.save_undo_record({
        "token": token,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_id": "act_conc_01",
        "action_type": "request_pause",
        "target_device": "dev_firetv_01",
        "status": "PENDING",
        "created_at": now,
        "expires_at": now + 600,
    })

    # Worker 1 acquires the lease
    acquired_1, status_1, _ = store.acquire_undo_lease(
        undo_token=token,
        user_id=user_id,
        lease_owner_id="worker_alpha",
        lease_duration=30.0,
        tenant_id=tenant_id,
    )
    assert acquired_1 is True

    # Worker 2 attempts concurrent lease acquisition while lease is active
    acquired_2, status_2, err_2 = store.acquire_undo_lease(
        undo_token=token,
        user_id=user_id,
        lease_owner_id="worker_beta",
        lease_duration=30.0,
        tenant_id=tenant_id,
    )
    assert acquired_2 is False
    assert status_2 == 409
    assert "currently in progress" in (err_2 or "")

    # Worker 1 completes the undo successfully
    completed = store.complete_undo(token, user_id, "worker_alpha")
    assert completed is True

    # Worker 2 or subsequent caller attempts again -> gets idempotent 200, NOT 409 or duplicate reversal
    acquired_3, status_3, err_3 = store.acquire_undo_lease(
        undo_token=token,
        user_id=user_id,
        lease_owner_id="worker_gamma",
        lease_duration=30.0,
        tenant_id=tenant_id,
    )
    assert acquired_3 is False
    assert status_3 == 200
    assert "already reversed" in (err_3 or "")


# ---------------------------------------------------------------------------
# Test 9: DynamoDB write failures do not result in a falsely successful issuance
# ---------------------------------------------------------------------------

def test_dynamodb_write_failure_does_not_result_in_falsely_successful_issuance():
    """When durable storage fails during token issuance, an honest 503 is returned."""
    table = RealisticMockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_alice"
    tenant_id = "tenant_test"

    # Set up valid consent grant
    now = time.time()
    store.grants["grant_test_01"] = {
        "grant_id": "grant_test_01",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "action_type": "request_pause",
        "scope": "allow_always",
        "target_allowlist": ["dev_firetv_living_room"],
        "active": True,
        "created_at": now,
    }

    # Simulate storage failure
    table.should_fail = True

    signal_body = {
        "user_id": user_id,
        "session_id": "sess_test_fail_01",
        "tenant_id": tenant_id,
        "explicit_signal": "self_report_distracted",
    }
    resp = handle_focus_signal(signal_body, store=store)

    # Must return 503 storage_error and must NOT return a successful undo token
    assert resp["statusCode"] == 503
    data = json.loads(resp["body"])
    assert data["error"] == "storage_error"
    assert data.get("undo_token") is None
    assert data.get("action_decision") == "execution_failed"


# ---------------------------------------------------------------------------
# Test 10: Caller-supplied tenant ID is never trusted without auth verification
# ---------------------------------------------------------------------------

def test_caller_supplied_tenant_id_never_trusted_by_http_handler():
    """HTTP router enforces that caller cannot forge tenant_id or user_id in body."""
    table = RealisticMockDynamoTable()
    store = DynamoDBDataStore(table=table)

    # Valid token belonging to tenant_victim
    store.save_undo_record({
        "token": "tok_victim_011",
        "userId": "usr_victim",
        "tenantId": "tenant_victim",
        "action_id": "act_vic_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    })

    # Attacker with valid claims for usr_attacker in tenant_attacker
    # attempts to pass body with user_id: usr_victim, tenant_id: tenant_victim
    attacker_jwt = auth_mod.create_test_jwt(user_id="usr_attacker", tenant_id="tenant_attacker")

    event = {
        "httpMethod": "POST",
        "path": "/api/v1/actions/undo",
        "headers": {
            "Authorization": f"Bearer {attacker_jwt}",
            "Content-Type": "application/json",
        },
        "body": json.dumps({
            "undo_token": "tok_victim_011",
            "user_id": "usr_victim",
            "tenant_id": "tenant_victim",
        }),
    }

    resp = lambda_handler(event, store_override=store)
    # The user authorization enforcement rejects the mismatch between auth claims and body
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert data["error"] == "forbidden"


# ---------------------------------------------------------------------------
# Test 11: Slack status durable undo token issuance and write failure
# ---------------------------------------------------------------------------

def test_slack_status_undo_persistence_and_write_failure():
    """Slack status publication durably persists undo token and fails closed if write fails."""
    table = RealisticMockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_slack_test"
    workspace_id = "T012345678"
    tenant_id = "tenant_enterprise"

    # Consent grant for slack_status
    now = time.time()
    store.grants["grant_slack_01"] = {
        "grant_id": "grant_slack_01",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "action_type": "slack_status",
        "scope": "allow_session",
        "target_allowlist": ["#marcus-focus"],
        "active": True,
        "created_at": now,
    }

    # Case 1: Storage failure during issuance
    table.should_fail = True
    pub_body = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "channel_id": "#marcus-focus",
        "custom_status": "Deep focus on architecture",
        "status_emoji": ":technologist:",
        "duration_minutes": 45,
    }
    fail_resp = handle_publish_slack_status(pub_body, store=store)
    assert fail_resp["statusCode"] == 503
    fail_data = json.loads(fail_resp["body"])
    assert fail_data["error"] == "storage_error"

    # Case 2: Storage restored, publication succeeds and durably persists undo record
    table.should_fail = False
    ok_resp = handle_publish_slack_status(pub_body, store=store)
    assert ok_resp["statusCode"] == 200
    ok_data = json.loads(ok_resp["body"])
    undo_tok = ok_data.get("undo_token")
    assert undo_tok is not None

    # Check durable table contains PENDING record
    durable_rec = store.get_undo_record(undo_tok)
    assert durable_rec is not None
    assert durable_rec["status"] == "PENDING"
    assert durable_rec["userId"] == user_id
    assert durable_rec["tenantId"] == tenant_id

    # Case 3: Undo execution reverses status and updates status to SUCCEEDED
    undo_resp = handle_undo_action(
        {"user_id": user_id, "tenant_id": tenant_id, "undo_token": undo_tok},
        store=store,
    )
    assert undo_resp["statusCode"] == 200
    undo_data = json.loads(undo_resp["body"])
    assert undo_data["success"] is True

    # Re-verify durable state in table
    updated_rec = store.get_undo_record(undo_tok)
    assert updated_rec is not None
    assert updated_rec["status"] == "SUCCEEDED"
