"""Batch 1.2A Security Gap Closure Regression Tests.

Finding 1 - High: Missing stored tenant ownership
  - undo_record with None/empty/whitespace-only tenantId is rejected (403), not allowed
  - audit-record fallback with missing tenant is also rejected (403)
  - tenant_default is never substituted for a missing stored tenant
  - Cross-tenant and cross-user requests blocked; same-user/same-tenant allowed

Finding 2 - Medium: Side-effect succeeds but undo persistence fails
  - Batch routine: reversal_attempted and reversal_ok fields in 503 response
  - Consent/device pause: same recovery fields
  - Slack status: same recovery fields

Finding 3 - Low: Schema contract
  - UndoRecord entity present in table_schema.json
  - All mandatory fields documented
"""
from __future__ import annotations

import importlib
import json
import time
from typing import Any
import pytest

cp = importlib.import_module("lambda.control_plane")
InMemoryDataStore = cp.InMemoryDataStore
DynamoDBDataStore = cp.DynamoDBDataStore
StorageError = cp.StorageError
handle_undo_action = cp.handle_undo_action
handle_focus_signal = cp.handle_focus_signal
handle_publish_slack_status = cp.handle_publish_slack_status

from standalone_agent.device_adapter import DeviceInfo, DeviceSimulatorAdapter


class MockDynamoTable:
    """Minimal mock DynamoDB table; can be set to fail on any write."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.should_fail_on_write: bool = False

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
        if self.should_fail_on_write:
            raise RuntimeError("DynamoDB service unavailable (simulated)")
        pk, sk = Item.get("PK", ""), Item.get("SK", "")
        existing = self.items.get((pk, sk))
        if ConditionExpression == "attribute_not_exists(PK)" and existing is not None:
            raise Exception("ConditionalCheckFailedException: Item already exists")
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
        if self.should_fail_on_write:
            raise RuntimeError("DynamoDB service unavailable (simulated)")
        pk, sk = Key.get("PK", ""), Key.get("SK", "")
        existing = self.items.get((pk, sk))
        if not existing:
            raise Exception("ResourceNotFoundException: Item not found")
        if ConditionExpression and "#status = :in_progress AND leaseOwnerId = :owner" in ConditionExpression:
            req_owner = (ExpressionAttributeValues or {}).get(":owner")
            if existing.get("status") != "IN_PROGRESS" or existing.get("leaseOwnerId") != req_owner:
                raise Exception("ConditionalCheckFailedException: Fencing token mismatch")
        vals = ExpressionAttributeValues or {}
        if ":step_list" in vals:
            current_revs = list(existing.get("completed_reversals") or [])
            for s in vals[":step_list"]:
                if s not in current_revs:
                    current_revs.append(s)
            existing["completed_reversals"] = current_revs
        elif ":succeeded" in vals:
            existing["status"] = "SUCCEEDED"
            existing["completedAt"] = int(time.time())
            if ":completed_revs" in vals:
                existing["completed_reversals"] = list(vals[":completed_revs"])
        elif ":failed" in vals:
            existing["status"] = "FAILED"
            existing["failedReason"] = vals.get(":reason", "failed")
            if ":completed_revs" in vals:
                existing["completed_reversals"] = list(vals[":completed_revs"])
        self.items[(pk, sk)] = existing
        return {}


# ===========================================================================
# Finding 1: Missing stored tenant ownership
# ===========================================================================

def test_undo_record_with_null_tenant_is_rejected_with_403():
    """An undo record with tenantId=None (missing) must fail closed with 403."""
    store = InMemoryDataStore()
    token = "tok_null_tenant_001"
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": None,
        "user_id": "usr_alice",
        "tenant_id": None,
        "action_id": "act_01",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_default", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403, "Missing stored tenant must be rejected with 403"
    data = json.loads(resp["body"])
    assert data["error"] == "unauthorized"
    assert "no stored tenant" in data["message"]


def test_undo_record_with_empty_string_tenant_is_rejected_with_403():
    """An undo record with tenantId='' (empty string) must fail closed with 403."""
    store = InMemoryDataStore()
    token = "tok_empty_tenant_002"
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": "",
        "user_id": "usr_alice",
        "tenant_id": "",
        "action_id": "act_02",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_default", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert data["error"] == "unauthorized"
    assert "no stored tenant" in data["message"]


def test_undo_record_with_whitespace_only_tenant_is_rejected_with_403():
    """An undo record with tenantId='   ' (whitespace only) must fail closed with 403."""
    store = InMemoryDataStore()
    token = "tok_whitespace_tenant_003"
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": "   ",
        "user_id": "usr_alice",
        "tenant_id": "   ",
        "action_id": "act_03",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_default", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert data["error"] == "unauthorized"
    assert "no stored tenant" in data["message"]


def test_tenant_default_is_not_substituted_for_missing_stored_tenant():
    """A caller with tenant_default MUST NOT be allowed to undo a record with no stored tenant.

    Core regression: before the fix, str(None or 'tenant_default') evaluated to 'tenant_default',
    silently matching the caller. After the fix this must reject with 403.
    """
    store = InMemoryDataStore()
    token = "tok_no_stored_tenant_004"
    # Record has NO tenantId or tenant_id at all (both absent from dict)
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "user_id": "usr_alice",
        "action_id": "act_04",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_default", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403, (
        "tenant_default must NOT be substituted for missing stored tenant; "
        f"expected 403 but got {resp['statusCode']}"
    )
    data = json.loads(resp["body"])
    assert data["error"] == "unauthorized"


def test_no_lease_acquired_when_stored_tenant_missing():
    """Handler must fail closed before acquiring a lease when stored tenant is missing."""
    store = InMemoryDataStore()
    token = "tok_no_lease_005"
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": None,
        "user_id": "usr_alice",
        "tenant_id": None,
        "action_id": "act_05",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_default", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403
    # Token must still be PENDING - no lease acquired
    rec = store.get_undo_record(token)
    assert rec is not None
    assert rec.get("status") == "PENDING", "Lease must NOT have been acquired for token with missing stored tenant"


def test_audit_record_fallback_with_missing_tenant_is_rejected():
    """Audit-record fallback also rejects when tenant_id is absent from stored audit record."""
    store = InMemoryDataStore()
    token = "tok_audit_no_tenant_006"
    # No undo_records entry - only an audit record with no tenant_id
    store.audit_records.append({
        "action_id": "act_06",
        "user_id": "usr_alice",
        # no tenant_id
        "action_type": "request_pause",
        "status": "executed",
        "undo_token": token,
        "timestamp": time.time(),
    })
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_default", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert data["error"] == "unauthorized"
    assert "no stored tenant" in data["message"]


def test_cross_tenant_request_blocked_with_populated_stored_tenant():
    """A caller with tenant_attacker cannot undo a record stored under tenant_victim."""
    store = InMemoryDataStore()
    token = "tok_cross_tenant_007"
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": "tenant_victim",
        "user_id": "usr_alice",
        "tenant_id": "tenant_victim",
        "action_id": "act_07",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_attacker", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert "another tenant" in data["message"]


def test_cross_user_request_blocked():
    """A different user within the same tenant cannot undo another user's token."""
    store = InMemoryDataStore()
    token = "tok_cross_user_008"
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": "tenant_corp",
        "user_id": "usr_alice",
        "tenant_id": "tenant_corp",
        "action_id": "act_08",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_bob", "tenant_id": "tenant_corp", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert "another user" in data["message"]


def test_valid_same_user_same_tenant_succeeds():
    """Legitimate same-user, same-tenant request is allowed through."""
    store = InMemoryDataStore()
    token = "tok_valid_owner_009"
    store.undo_records[token] = {
        "token": token,
        "userId": "usr_alice",
        "tenantId": "tenant_corp",
        "user_id": "usr_alice",
        "tenant_id": "tenant_corp",
        "action_id": "act_09",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_corp", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] not in (401, 403), (
        f"Valid same-user/same-tenant undo must not be blocked; got {resp['statusCode']}"
    )


def test_blank_user_in_stored_record_rejects_all_callers():
    """A stored record with empty/whitespace userId fails closed regardless of caller."""
    store = InMemoryDataStore()
    token = "tok_blank_user_010"
    store.undo_records[token] = {
        "token": token,
        "userId": "   ",
        "tenantId": "tenant_corp",
        "user_id": "   ",
        "tenant_id": "tenant_corp",
        "action_id": "act_10",
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 600,
    }
    resp = handle_undo_action(
        {"user_id": "usr_alice", "tenant_id": "tenant_corp", "undo_token": token},
        store=store,
    )
    assert resp["statusCode"] == 403
    data = json.loads(resp["body"])
    assert data["error"] == "unauthorized"


# ===========================================================================
# Finding 2: Side effect succeeds but undo persistence fails
# ===========================================================================

def test_batch_routine_persistence_failure_reports_reversal_outcome():
    """Batch routine path: 503 response includes reversal_attempted and reversal_ok fields."""
    table = MockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_alice"
    tenant_id = "tenant_corp"

    store.grants["grant_rtn_01"] = {
        "grant_id": "grant_rtn_01",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "action_type": "routine",
        "scope": "allow_always",
        "intent_pattern": "tired",
        "active": True,
        "created_at": time.time(),
    }
    table.should_fail_on_write = True

    signal_body = {
        "user_id": user_id,
        "session_id": "sess_batch_persist_fail_01",
        "tenant_id": tenant_id,
        "user_intent": "tired",
    }
    resp = handle_focus_signal(signal_body, store=store)

    assert resp["statusCode"] == 503
    data = json.loads(resp["body"])
    assert data["error"] == "storage_error"
    assert data.get("undo_token") is None, "undo_token must be None when persistence fails"
    assert "reversal_attempted" in data, "reversal_attempted must be present in 503"
    assert "reversal_ok" in data, "reversal_ok must be present in 503"


def test_consent_pause_persistence_failure_reports_reversal_outcome():
    """Consent/device-pause path: 503 response includes reversal_attempted and reversal_ok."""
    table = MockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_alice"
    tenant_id = "tenant_corp"

    now = time.time()
    store.grants["grant_pause_01"] = {
        "grant_id": "grant_pause_01",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "action_type": "request_pause",
        "scope": "allow_always",
        "target_allowlist": ["dev_firetv_living_room"],
        "active": True,
        "created_at": now,
    }
    table.should_fail_on_write = True

    signal_body = {
        "user_id": user_id,
        "session_id": "sess_consent_persist_fail_01",
        "tenant_id": tenant_id,
        "explicit_signal": "self_report_distracted",
    }
    resp = handle_focus_signal(signal_body, store=store)

    assert resp["statusCode"] == 503
    data = json.loads(resp["body"])
    assert data["error"] == "storage_error"
    assert data.get("undo_token") is None
    assert "reversal_attempted" in data
    assert "reversal_ok" in data


def test_slack_status_persistence_failure_reports_reversal_outcome():
    """Slack status path: 503 response includes reversal_attempted and reversal_ok."""
    table = MockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_slack_01"
    tenant_id = "tenant_enterprise"

    now = time.time()
    store.grants["grant_slack_02"] = {
        "grant_id": "grant_slack_02",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "action_type": "slack_status",
        "scope": "allow_session",
        "target_allowlist": ["#marcus-focus"],
        "active": True,
        "created_at": now,
    }
    table.should_fail_on_write = True

    pub_body = {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "workspace_id": "T012345678",
        "channel_id": "#marcus-focus",
        "custom_status": "In deep focus",
        "status_emoji": ":technologist:",
        "duration_minutes": 45,
    }
    resp = handle_publish_slack_status(pub_body, store=store)

    assert resp["statusCode"] == 503
    data = json.loads(resp["body"])
    assert data["error"] == "storage_error"
    assert "reversal_attempted" in data, "reversal_attempted field must be present"
    assert "reversal_ok" in data, "reversal_ok field must be present"
    assert isinstance(data["reversal_ok"], bool), "reversal_ok must be a boolean"


def test_persistence_failure_response_does_not_claim_rollback_succeeded():
    """No 503 response body may contain a message implying success when persistence fails."""
    table = MockDynamoTable()
    store = DynamoDBDataStore(table=table)
    user_id = "usr_alice"
    tenant_id = "tenant_corp"

    now = time.time()
    store.grants["grant_pause_honesty"] = {
        "grant_id": "grant_pause_honesty",
        "user_id": user_id,
        "tenant_id": tenant_id,
        "action_type": "request_pause",
        "scope": "allow_always",
        "target_allowlist": ["dev_firetv_living_room"],
        "active": True,
        "created_at": now,
    }
    table.should_fail_on_write = True

    resp = handle_focus_signal(
        {
            "user_id": user_id,
            "session_id": "sess_honesty_check",
            "tenant_id": tenant_id,
            "explicit_signal": "self_report_distracted",
        },
        store=store,
    )

    assert resp["statusCode"] == 503
    body_str = resp["body"].lower()
    assert "rollback succeeded" not in body_str
    assert "reversed successfully" not in body_str


# ===========================================================================
# Finding 3: Schema contract
# ===========================================================================

def _load_schema() -> dict[str, Any]:
    from pathlib import Path
    schema_path = Path(__file__).resolve().parent.parent.parent / "schemas" / "dynamodb" / "table_schema.json"
    with open(schema_path) as f:
        return json.load(f)


def test_undo_record_entity_exists_in_schema():
    """UndoRecord entity must be present in the DynamoDB schema."""
    schema = _load_schema()
    assert "UndoRecord" in schema.get("entities", {}), \
        "UndoRecord entity is missing from schemas/dynamodb/table_schema.json"


def test_undo_record_key_pattern_is_correct():
    """UndoRecord PK must be UNDO#{undo_token} and SK must be RECORD."""
    schema = _load_schema()
    undo_entity = schema["entities"]["UndoRecord"]
    assert undo_entity["PK"] == "UNDO#{undo_token}", f"Wrong PK: {undo_entity['PK']}"
    assert undo_entity["SK"] == "RECORD", f"Wrong SK: {undo_entity['SK']}"


def test_undo_record_mandatory_ownership_fields_documented():
    """UndoRecord must document userId and tenantId as mandatory/non-blank attributes."""
    schema = _load_schema()
    attrs = schema["entities"]["UndoRecord"].get("attributes", {})
    assert "userId" in attrs, "userId must be in UndoRecord attributes"
    assert "tenantId" in attrs, "tenantId must be in UndoRecord attributes"
    assert "non-blank" in attrs["userId"].lower() or "mandatory" in attrs["userId"].lower()
    assert "non-blank" in attrs["tenantId"].lower() or "mandatory" in attrs["tenantId"].lower()


def test_undo_record_status_documents_valid_transitions():
    """UndoRecord status attribute must document all allowed status values."""
    schema = _load_schema()
    attrs = schema["entities"]["UndoRecord"].get("attributes", {})
    assert "status" in attrs
    status_doc = attrs["status"]
    for expected in ("PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED"):
        assert expected in status_doc, f"status must document '{expected}'"


def test_undo_record_lease_fields_documented():
    """UndoRecord must document lease fields for concurrent-execution safety."""
    schema = _load_schema()
    attrs = schema["entities"]["UndoRecord"].get("attributes", {})
    assert "leaseOwnerId" in attrs
    assert "leaseExpiresAt" in attrs


def test_schema_is_valid_json():
    """Schema file must remain valid JSON after changes."""
    from pathlib import Path
    schema_path = Path(__file__).resolve().parent.parent.parent / "schemas" / "dynamodb" / "table_schema.json"
    with open(schema_path) as f:
        parsed = json.load(f)
    assert isinstance(parsed, dict)
    assert "entities" in parsed


def test_schema_tenant_default_policy_documented():
    """The UndoRecord notes must document the tenant_default non-substitution rule."""
    schema = _load_schema()
    undo_entity = schema["entities"]["UndoRecord"]
    notes = undo_entity.get("notes", [])
    notes_text = " ".join(notes).lower()
    assert "tenant_default" in notes_text, "Notes must mention tenant_default policy"
    assert "missing" in notes_text or "never" in notes_text, \
        "Notes must clarify missing tenantId is never substituted"


# ===========================================================================
# Batch 1.2A Remediation Tests: F-01 through F-05
# ===========================================================================

def test_slack_undo_boolean_and_dict_normalization():
    """F-01: SlackAdapter.execute_undo() returning bool or dict is normalized correctly."""
    store = InMemoryDataStore()
    user_id = "usr_slack_test"
    tenant_id = "tenant_test"
    token = "tok_slack_norm_01"

    store.undo_records[token] = {
        "token": token,
        "undo_token": token,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_type": "slack_status",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "retry_count": 0,
    }

    # Case A: SlackAdapter returns True (bool)
    store.slack_adapter.execute_undo = lambda tok: True  # type: ignore[assignment]
    resp = handle_undo_action({"undo_token": token, "user_id": user_id, "tenant_id": tenant_id}, store=store)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["success"] is True
    assert store.undo_records[token]["status"] == "SUCCEEDED"

    # Case B: SlackAdapter returns False (bool)
    token_fail = "tok_slack_norm_fail"
    store.undo_records[token_fail] = {
        "token": token_fail,
        "undo_token": token_fail,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_type": "slack_status",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "retry_count": 0,
    }
    store.slack_adapter.execute_undo = lambda tok: False  # type: ignore[assignment]
    resp_fail = handle_undo_action({"undo_token": token_fail, "user_id": user_id, "tenant_id": tenant_id}, store=store)
    assert resp_fail["statusCode"] == 502
    body_fail = json.loads(resp_fail["body"])
    assert body_fail["success"] is False
    assert store.undo_records[token_fail]["status"] == "FAILED"

    # Case C: SlackAdapter returns dict {"success": True}
    token_dict = "tok_slack_dict_ok"
    store.undo_records[token_dict] = {
        "token": token_dict,
        "undo_token": token_dict,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_type": "slack_status",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "retry_count": 0,
    }
    store.slack_adapter.execute_undo = lambda tok: {"success": True, "cleared": True}  # type: ignore[assignment]
    resp_dict = handle_undo_action({"undo_token": token_dict, "user_id": user_id, "tenant_id": tenant_id}, store=store)
    assert resp_dict["statusCode"] == 200
    assert json.loads(resp_dict["body"])["success"] is True


def test_failed_device_undo_never_reported_as_succeeded():
    """F-02: Device undo failure must return 502 and transition token to FAILED, not 200."""
    store = InMemoryDataStore()
    user_id = "usr_dev_fail"
    tenant_id = "tenant_test"
    token = "tok_dev_fail_01"

    store.undo_records[token] = {
        "token": token,
        "undo_token": token,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_type": "request_pause",
        "target_device": "dev_speaker_01",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "retry_count": 0,
    }

    # Simulate device failure (not offline, e.g. command rejection)
    store.device_adapter.execute_undo = lambda tok: {"success": False, "message": "Command rejected by firmware"}  # type: ignore[assignment]
    resp = handle_undo_action({"undo_token": token, "user_id": user_id, "tenant_id": tenant_id}, store=store)

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["success"] is False
    assert body["retryable"] is True
    assert store.undo_records[token]["status"] == "FAILED"
    assert store.undo_records[token]["retry_count"] == 1


def test_adapter_exception_handled_honestly_as_failure():
    """F-02: Unhandled adapter exceptions must be caught, recorded as FAILED, and return 502."""
    store = InMemoryDataStore()
    user_id = "usr_exc_test"
    tenant_id = "tenant_test"
    token = "tok_adapter_exc_01"

    store.undo_records[token] = {
        "token": token,
        "undo_token": token,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_type": "request_pause",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "retry_count": 0,
    }

    def raise_exc(tok):
        raise ConnectionResetError("Socket reset by peer")

    store.device_adapter.execute_undo = raise_exc  # type: ignore[assignment]
    resp = handle_undo_action({"undo_token": token, "user_id": user_id, "tenant_id": tenant_id}, store=store)

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["success"] is False
    assert body["retryable"] is True
    assert store.undo_records[token]["status"] == "FAILED"


def test_direct_acquire_undo_lease_rejects_missing_tenant():
    """F-03: Direct acquire_undo_lease calls must reject missing/blank tenant and never substitute tenant_default."""
    store = InMemoryDataStore()
    token = "tok_direct_no_tenant"
    user_id = "usr_direct_01"

    # Stored record has user_id but missing tenantId and tenant_id
    store.undo_records[token] = {
        "token": token,
        "userId": user_id,
        "tenantId": "",
        "tenant_id": "",
        "status": "PENDING",
        "leaseOwnerId": "",
        "leaseExpiresAt": 0,
    }

    # Direct call attempting to acquire lease
    acquired, code, msg = store.acquire_undo_lease(token, user_id, "worker_direct", tenant_id="tenant_default")
    assert acquired is False
    assert code == 403
    assert "no stored tenant" in msg.lower()
    # Ensure no mutation occurred
    assert store.undo_records[token]["status"] == "PENDING"
    assert store.undo_records[token]["leaseOwnerId"] == ""


def test_legacy_record_compatibility_snake_and_camel_case():
    """F-04: Existing records with snake_case attributes acquire lease successfully; missing ownership fails closed."""
    mock_table = MockDynamoTable()
    store = DynamoDBDataStore(table=mock_table)
    user_id = "usr_legacy_user"
    tenant_id = "tenant_legacy"
    token = "tok_legacy_snake_case"

    # Persist a legacy record using ONLY snake_case attributes
    mock_table.items[("UNDO#" + token, "RECORD")] = {
        "PK": f"UNDO#{token}",
        "SK": "RECORD",
        "token": token,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "status": "PENDING",
        "created_at": int(time.time()),
        "expires_at": int(time.time() + 86400),
    }

    # Lease acquisition must succeed using backward-compatible condition
    acquired, code, err = store.acquire_undo_lease(token, user_id, "worker_leg_1", tenant_id=tenant_id)
    assert acquired is True
    assert code == 200

    # Ensure updated record in table now contains both camelCase and snake_case
    updated = mock_table.items[("UNDO#" + token, "RECORD")]
    assert updated["userId"] == user_id
    assert updated["tenantId"] == tenant_id
    assert updated["status"] == "IN_PROGRESS"


def test_schema_documents_completed_at_and_failed_reason():
    """F-05: table_schema.json documents completedAt, failedReason, and retry_count."""
    schema = _load_schema()
    attrs = schema["entities"]["UndoRecord"]["attributes"]
    assert "completedAt" in attrs, "completedAt must be in UndoRecord attributes"
    assert "failedReason" in attrs, "failedReason must be in UndoRecord attributes"
    assert "retry_count" in attrs, "retry_count must be in UndoRecord attributes"

    notes = " ".join(schema["entities"]["UndoRecord"].get("notes", [])).lower()
    assert "completedat" in notes, "Notes must describe completedAt"
    assert "failedreason" in notes, "Notes must describe failedReason"
    assert "retry_count" in notes, "Notes must describe retry_count atomic increment"


def test_fail_undo_increments_retry_count_consistently():
    """F-05: fail_undo increments retry_count atomically in both in-memory and DynamoDB stores."""
    # In-memory test
    mem_store = InMemoryDataStore()
    tok_mem = "tok_retry_mem"
    mem_store.undo_records[tok_mem] = {
        "status": "IN_PROGRESS",
        "leaseOwnerId": "owner_mem",
        "retry_count": 2,
    }
    mem_store.fail_undo(tok_mem, "usr_1", "owner_mem", error_reason="test_fail")
    assert mem_store.undo_records[tok_mem]["status"] == "FAILED"
    assert mem_store.undo_records[tok_mem]["retry_count"] == 3
    assert mem_store.undo_records[tok_mem]["failedReason"] == "test_fail"

    # DynamoDB test
    mock_table = MockDynamoTable()
    dyn_store = DynamoDBDataStore(table=mock_table)
    tok_dyn = "tok_retry_dyn"
    mock_table.items[("UNDO#" + tok_dyn, "RECORD")] = {
        "status": "IN_PROGRESS",
        "leaseOwnerId": "owner_dyn",
        "retry_count": 1,
    }
    dyn_store.undo_records[tok_dyn] = dict(mock_table.items[("UNDO#" + tok_dyn, "RECORD")])
    dyn_store.fail_undo(tok_dyn, "usr_1", "owner_dyn", error_reason="test_dyn_fail")
    assert dyn_store.undo_records[tok_dyn]["status"] == "FAILED"
    assert dyn_store.undo_records[tok_dyn]["retry_count"] == 2


# ===========================================================================
# Overnight Verification Regressions: Batch Offline, Tenancy & Schema
# ===========================================================================

def test_batch_device_undo_offline_truthful_failure():
    """Batch undo with an offline sub-device must truthfully report device_offline and HTTP 502."""
    store = InMemoryDataStore()
    user_id = "usr_batch_offline_01"
    tenant_id = "tenant_test_batch"

    # Register two devices online
    dev1 = DeviceInfo(
        device_id="dev_speaker_online",
        name="Speaker Online",
        device_type="fire_tv",
        capabilities={"transport_control", "pause_playback", "resume_playback"},
        state="playing",
        is_online=True,
    )
    dev2 = DeviceInfo(
        device_id="dev_speaker_offline",
        name="Speaker Offline",
        device_type="fire_tv",
        capabilities={"transport_control", "pause_playback", "resume_playback"},
        state="playing",
        is_online=True,
    )
    store.device_adapter.register_device(dev1)
    store.device_adapter.register_device(dev2)

    # Execute batch of pause commands while both are online
    batch_res = store.device_adapter.execute_batch([
        {"device_id": "dev_speaker_online", "action": "request_pause"},
        {"device_id": "dev_speaker_offline", "action": "request_pause"},
    ])
    batch_undo_token = batch_res["batch_undo_token"]
    assert len(store.device_adapter.batch_undo_map[batch_undo_token]) == 2

    # Now dev2 goes offline / loses network connectivity
    dev2.is_online = False

    # Undo execution directly on adapter must indicate offline failure
    undo_res = store.device_adapter.execute_undo(batch_undo_token)
    assert undo_res["success"] is False
    assert undo_res.get("offline") is True
    assert undo_res.get("error") == "device_offline"

    # Persist durable undo record for the batch action
    store.undo_records[batch_undo_token] = {
        "token": batch_undo_token,
        "undo_token": batch_undo_token,
        "userId": user_id,
        "tenantId": tenant_id,
        "action_type": "request_pause",
        "target_device": "dev_speaker_offline",
        "status": "PENDING",
        "created_at": time.time(),
        "expires_at": time.time() + 3600,
        "retry_count": 0,
    }

    # Calling handle_undo_action must return 502 with truthful device_reversal_failed
    http_resp = handle_undo_action({
        "undo_token": batch_undo_token,
        "user_id": user_id,
        "tenant_id": tenant_id,
    }, store=store)

    assert http_resp["statusCode"] == 502
    body = json.loads(http_resp["body"])
    assert body["success"] is False
    assert body["error"] == "device_reversal_failed"
    assert body["retryable"] is True
    assert "Device offline" in body["message"]


def test_batch_device_undo_idempotent_recovery_when_device_online():
    """Once an offline device reconnects, retrying the batch undo must succeed idempotently."""
    adapter = DeviceSimulatorAdapter()
    dev1 = DeviceInfo(
        device_id="dev_recov_1",
        name="Living Room TV",
        device_type="fire_tv",
        capabilities={"transport_control", "pause_playback", "resume_playback"},
        state="playing",
        is_online=True,
    )
    dev2 = DeviceInfo(
        device_id="dev_recov_2",
        name="Bedroom TV",
        device_type="fire_tv",
        capabilities={"transport_control", "pause_playback", "resume_playback"},
        state="playing",
        is_online=True,
    )
    adapter.register_device(dev1)
    adapter.register_device(dev2)

    batch_res = adapter.execute_batch([
        {"device_id": "dev_recov_1", "action": "request_pause"},
        {"device_id": "dev_recov_2", "action": "request_pause"},
    ])
    tok = batch_res["batch_undo_token"]
    assert len(adapter.batch_undo_map[tok]) == 2

    # dev2 loses connection
    dev2.is_online = False

    # Initial attempt fails because dev2 is offline
    first_attempt = adapter.execute_undo(tok)
    assert first_attempt["success"] is False
    assert first_attempt.get("offline") is True

    # Bring dev2 back online
    dev2.is_online = True

    # Retry must succeed idempotently without re-inverting dev1
    retry_attempt = adapter.execute_undo(tok)
    assert retry_attempt["success"] is True
    assert dev1.state == "playing"
    assert dev2.state == "playing"
    retry_attempt = adapter.execute_undo(tok)
    assert retry_attempt["success"] is True
    assert dev1.state == "playing"
    assert dev2.state == "playing"


def test_inmemory_datastore_cross_tenant_isolation():
    """InMemoryDataStore must isolate resources across non-default tenants."""
    store = InMemoryDataStore()
    user_id = "usr_tenant_iso_01"

    # Save grant in tenant_alpha
    store.save_grant({
        "grant_id": "grant_alpha_1",
        "user_id": user_id,
        "tenant_id": "tenant_alpha",
        "action_type": "request_pause",
        "scope": "allow_always",
        "active": True,
    })

    # Querying tenant_alpha finds the grant
    alpha_grants = store.get_grants(user_id, tenant_id="tenant_alpha")
    assert len(alpha_grants) == 1
    assert alpha_grants[0]["grant_id"] == "grant_alpha_1"

    # Querying tenant_beta returns empty (isolated)
    beta_grants = store.get_grants(user_id, tenant_id="tenant_beta")
    assert len(beta_grants) == 0

    # Foreign tenant cannot revoke grant
    assert store.revoke_grant("grant_alpha_1", user_id, tenant_id="tenant_beta") is False
    assert len(store.get_grants(user_id, tenant_id="tenant_alpha")) == 1

    # Rightful tenant successfully revokes grant
    assert store.revoke_grant("grant_alpha_1", user_id, tenant_id="tenant_alpha") is True
    assert len(store.get_grants(user_id, tenant_id="tenant_alpha")) == 0


def test_inmemory_focus_session_user_isolation():
    """InMemoryDataStore.get_focus_session must isolate sessions across distinct user IDs."""
    store = InMemoryDataStore()
    store.save_focus_session({
        "session_id": "sess_alice_private",
        "user_id": "usr_alice",
        "status": "active",
    })

    # Alice accesses her own session
    assert store.get_focus_session("usr_alice", "sess_alice_private") is not None

    # Bob attempting to read Alice's session returns None
    assert store.get_focus_session("usr_bob", "sess_alice_private") is None


def test_dynamodb_schema_user_preferences_conformance():
    """table_schema.json documents UserPreferences entity mapping matching single-table design."""
    schema = _load_schema()
    assert "UserPreferences" in schema["entities"]
    pref_entity = schema["entities"]["UserPreferences"]
    assert pref_entity["PK"] == "USER#{user_id}"
    assert pref_entity["SK"] == "PREFERENCES"
    assert "preferences" in pref_entity["attributes"]
    assert "ttl" in pref_entity["attributes"]
