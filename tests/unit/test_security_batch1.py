"""Approved Security Fix Batch 1 & 1.1 Regression Suite.

Verifies:
1. Tenant authorization bypass prevention:
   - A caller with tenant_default cannot select a different tenant.
   - A caller with a valid tenant can access its own resources.
2. WebSocket cryptographic authentication and user-to-connection binding:
   - Invalid, forged, or expired tokens are rejected (403).
   - Missing connection IDs and absent authorizer claims fail closed with 403.
   - Absent connection records fail closed with 403.
   - Unavailable connection storage fails closed with 403.
   - WebSocket relay messages cannot impersonate another user.
   - WebSocket relay messages without authenticated identity fail closed with 403.
3. Adaptive-routine ownership — fail closed:
   - Authenticated owner can update and revoke their own routine.
   - A different authenticated user cannot update or revoke that routine (403 Forbidden).
   - Ownerless legacy routines reject update and revocation (403 Forbidden).
   - Unauthenticated callers reject update and revocation (401 Unauthorized).
4. Test-only authentication bypass protection:
   - In live AWS Lambda environments (e.g. AWS_LAMBDA_FUNCTION_NAME set), test bypass
     cannot be activated even if allow_test_bypass=True and request contains user_id/tenant_id.
"""
import importlib
import json
import os
import time
from unittest.mock import MagicMock
import pytest

# Lambda modules imported via importlib as required by repo conventions
_auth = importlib.import_module("lambda.auth")
_control_plane = importlib.import_module("lambda.control_plane")
_ws = importlib.import_module("lambda.websocket_handler")

AuthContext = _auth.AuthContext
AuthError = _auth.AuthError
enforce_tenant_authorization = _auth.enforce_tenant_authorization
extract_and_validate_auth = _auth.extract_and_validate_auth
create_test_jwt = _auth.create_test_jwt

InMemoryDataStore = _control_plane.InMemoryDataStore
handle_create_routine = _control_plane.handle_create_routine
handle_update_routine = _control_plane.handle_update_routine
handle_revoke_routine = _control_plane.handle_revoke_routine
cp_lambda_handler = _control_plane.lambda_handler

ws_validate_connect_token = _ws.validate_connect_token
ws_handle_connect = _ws.handle_connect
ws_handle_relay = _ws.handle_relay
ws_lambda_handler = _ws.lambda_handler


# ===========================================================================
# 1. Tenant Authorization Tests
# ===========================================================================

def test_tenant_default_cannot_select_different_tenant():
    """A caller with tenant_default cannot adopt an arbitrary target tenant."""
    ctx = AuthContext(user_id="usr_normal", tenant_id="tenant_default", is_authenticated=True)
    with pytest.raises(AuthError) as exc_info:
        enforce_tenant_authorization(ctx, target_tenant_id="tenant_victim_enterprise")
    assert exc_info.value.status_code == 403
    assert exc_info.value.error == "forbidden"
    assert "cross_tenant_access_blocked" in str(exc_info.value.message) or "Access denied" in str(exc_info.value.message)


def test_tenant_default_can_access_own_default_resources():
    """A caller with tenant_default can legitimately access tenant_default or omit target."""
    ctx = AuthContext(user_id="usr_normal", tenant_id="tenant_default", is_authenticated=True)
    assert enforce_tenant_authorization(ctx, None) == "tenant_default"
    assert enforce_tenant_authorization(ctx, "tenant_default") == "tenant_default"


def test_valid_tenant_can_access_own_resources():
    """A valid tenant can access its own resources but not another tenant."""
    ctx = AuthContext(user_id="usr_corp", tenant_id="tenant_corp_123", is_authenticated=True)
    # Own resources
    assert enforce_tenant_authorization(ctx, "tenant_corp_123") == "tenant_corp_123"
    assert enforce_tenant_authorization(ctx, None) == "tenant_corp_123"

    # Cross-tenant attempt blocked
    with pytest.raises(AuthError) as exc_info:
        enforce_tenant_authorization(ctx, "tenant_corp_999")
    assert exc_info.value.status_code == 403
    assert exc_info.value.error == "forbidden"


# ===========================================================================
# 2. WebSocket Cryptographic Authentication and User-Binding Tests
# ===========================================================================

def test_invalid_or_forged_websocket_tokens_are_rejected():
    """Forged, tampered, expired, and garbage tokens are strictly rejected."""
    # 1. Tampered cryptographic signature
    tampered = create_test_jwt("usr_alice", tamper_signature=True)
    valid, _ = ws_validate_connect_token(f"Bearer {tampered}")
    assert valid is False

    # 2. Expired token
    expired = create_test_jwt("usr_alice", expires_in_seconds=-50)
    valid, _ = ws_validate_connect_token(f"Bearer {expired}")
    assert valid is False

    # 3. Forged non-JWT token
    valid, _ = ws_validate_connect_token("Bearer forged_token_without_signature")
    assert valid is False

    # 4. Valid token succeeds
    valid_token = create_test_jwt("usr_alice")
    valid, claims = ws_validate_connect_token(f"Bearer {valid_token}")
    assert valid is True
    assert claims["user_id"] == "usr_alice"


def test_websocket_relay_cannot_impersonate_another_user():
    """A relay message cannot specify a user_id different from the connection's authenticated user."""
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_relay_attack",
        "event_type": "focus_signal",
        "user_id": "usr_victim",
        "timestamp": time.time(),
        "signal_payload": {"explicit_signal": "self_report_distracted"},
    }

    # Connection was authenticated as usr_attacker; event payload claims usr_victim
    resp = ws_handle_relay(
        body,
        bus_client=mock_bus,
        authenticated_user_id="usr_attacker",
        connection_id="conn_attacker_1",
    )
    assert resp["statusCode"] == 403
    assert resp["body"]["error"] == "forbidden"
    assert "cannot impersonate" in resp["body"]["message"]
    mock_bus.put_events.assert_not_called()


def test_websocket_relay_without_authenticated_identity_fails_closed():
    """Relay requests with no authenticated identity fail closed with 403."""
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_unauth_relay",
        "event_type": "focus_signal",
        "user_id": "usr_claimed_attacker",
        "timestamp": time.time(),
    }
    # Invocation without authenticated_user_id
    resp = ws_handle_relay(body, bus_client=mock_bus, authenticated_user_id=None)
    assert resp["statusCode"] == 403
    assert resp["body"]["error"] == "forbidden"
    mock_bus.put_events.assert_not_called()


def test_websocket_relay_with_authenticated_owner_succeeds():
    """Relay succeeds when user_id matches authenticated connection identity."""
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_relay_valid",
        "event_type": "focus_signal",
        "user_id": "usr_legit",
        "timestamp": time.time(),
        "signal_payload": {"explicit_signal": "self_report_distracted"},
    }
    resp = ws_handle_relay(
        body,
        bus_client=mock_bus,
        authenticated_user_id="usr_legit",
        connection_id="conn_legit_1",
    )
    assert resp["statusCode"] == 200
    assert resp["body"]["status"] == "accepted"
    mock_bus.put_events.assert_called_once()


def test_websocket_relay_lambda_handler_fails_closed_on_missing_or_absent_records():
    """lambda_handler fails closed across absent connection ID, missing table record, and unavailable storage."""
    mock_bus = MagicMock()
    mock_table = MagicMock()

    # 1. Missing connection ID
    event_no_cid = {
        "requestContext": {"routeKey": "relay"},
        "body": json.dumps({"action": "relay", "event_id": "e1", "user_id": "u1", "event_type": "focus_signal"}),
    }
    res1 = ws_lambda_handler(event_no_cid, table_override=mock_table, bus_override=mock_bus)
    assert res1["statusCode"] == 403

    # 2. Unavailable storage (table is None)
    event_with_cid = {
        "requestContext": {"routeKey": "relay", "connectionId": "conn_no_storage"},
        "body": json.dumps({"action": "relay", "event_id": "e2", "user_id": "u1", "event_type": "focus_signal"}),
    }
    res2 = ws_lambda_handler(event_with_cid, table_override=None, bus_override=mock_bus)
    assert res2["statusCode"] == 403

    # 3. Absent connection record in DynamoDB
    mock_table.get_item.return_value = {}  # No Item
    res3 = ws_lambda_handler(event_with_cid, table_override=mock_table, bus_override=mock_bus)
    assert res3["statusCode"] == 403


# ===========================================================================
# 3. Object-Level Authorization for Routine Updates Tests
# ===========================================================================

def test_authenticated_owner_can_update_their_routine():
    """Routine owner can update the name, intent, and actions of their routine."""
    store = InMemoryDataStore()
    create_payload = {
        "user_id": "usr_owner",
        "intent": "Focus sprint",
        "actions": [{"device": "dev_smart_lights", "action": "dim"}],
        "name": "Focus Sprint Routine",
    }
    res = handle_create_routine(create_payload, store=store)
    assert res["statusCode"] == 201
    created = json.loads(res["body"])
    routine_id = created["routine_id"]

    # Owner updates the routine
    update_res = handle_update_routine(
        routine_id,
        {
            "user_id": "usr_owner",
            "name": "Renamed Sprint Routine",
            "actions": [{"device": "dev_smart_lights", "action": "deep_dim"}],
        },
        store=store,
        caller_user_id="usr_owner",
    )
    assert update_res["statusCode"] == 200
    updated_routine = json.loads(update_res["body"])["routine"]
    assert updated_routine["name"] == "Renamed Sprint Routine"
    assert updated_routine["actions"][0]["action"] == "deep_dim"


def test_different_authenticated_user_cannot_update_routine():
    """A different user attempting to update another user's routine is blocked with 403 Forbidden."""
    store = InMemoryDataStore()
    create_payload = {
        "user_id": "usr_alice",
        "intent": "Meditation prep",
        "actions": [{"device": "dev_speaker", "action": "play_ambient"}],
        "name": "Alice Meditation",
    }
    res = handle_create_routine(create_payload, store=store)
    assert res["statusCode"] == 201
    routine_id = json.loads(res["body"])["routine_id"]

    # Attacker Bob attempts to update Alice's routine
    update_res = handle_update_routine(
        routine_id,
        {
            "name": "Tampered Routine by Bob",
            "actions": [{"device": "dev_speaker", "action": "play_loud_noise"}],
        },
        store=store,
        caller_user_id="usr_bob",
    )
    assert update_res["statusCode"] == 403
    body = json.loads(update_res["body"])
    assert body["error"] == "forbidden"
    assert "Access denied" in body["message"]

    # Verify Alice's routine was not modified
    assert store.learned_routines[routine_id]["name"] == "Alice Meditation"


def test_ownerless_routine_fails_closed_on_update_and_revoke():
    """Ownerless routine records reject update and revocation with 403 Forbidden."""
    store = InMemoryDataStore()
    # Insert an ownerless legacy routine
    store.learned_routines["rtn_legacy_ownerless"] = {
        "routine_id": "rtn_legacy_ownerless",
        "user_id": None,
        "name": "Legacy Unowned Routine",
        "intent": "legacy",
        "actions": [],
        "active": True,
    }

    # Attempt update by any caller -> 403 Forbidden
    up_res = handle_update_routine(
        "rtn_legacy_ownerless",
        {"name": "Hijacked"},
        store=store,
        caller_user_id="usr_any",
    )
    assert up_res["statusCode"] == 403
    assert "has no assigned owner" in json.loads(up_res["body"])["message"]

    # Attempt revoke by any caller -> 403 Forbidden
    del_res = handle_revoke_routine(
        "rtn_legacy_ownerless",
        user_id="usr_any",
        store=store,
    )
    assert del_res["statusCode"] == 403
    assert "has no assigned owner" in json.loads(del_res["body"])["message"]


def test_unauthenticated_caller_cannot_modify_routine():
    """Unauthenticated caller (no user_id) cannot update or revoke routine."""
    store = InMemoryDataStore()
    create_payload = {
        "user_id": "usr_alice",
        "intent": "Reading",
        "actions": [],
        "name": "Alice Reading",
    }
    create_res = handle_create_routine(create_payload, store=store)
    routine_id = json.loads(create_res["body"])["routine_id"]

    # Update without user_id -> 401
    up_res = handle_update_routine(routine_id, {"name": "No Auth"}, store=store, caller_user_id=None)
    assert up_res["statusCode"] == 401

    # Revoke without user_id -> 401
    del_res = handle_revoke_routine(routine_id, user_id="", store=store)
    assert del_res["statusCode"] == 401


def test_control_plane_http_router_enforces_routine_ownership():
    """PUT /api/v1/routines/{id} via HTTP control plane checks caller token ownership."""
    store = InMemoryDataStore()
    create_payload = {
        "user_id": "usr_alice",
        "intent": "Deep work",
        "actions": [{"device": "dev_nudge", "action": "vibrate"}],
        "name": "Alice Deep Work",
    }
    create_res = handle_create_routine(create_payload, store=store)
    routine_id = json.loads(create_res["body"])["routine_id"]

    # Bob sends request with Bob's JWT
    bob_token = create_test_jwt("usr_bob")
    event = {
        "path": f"/api/v1/routines/{routine_id}",
        "httpMethod": "PUT",
        "headers": {"Authorization": f"Bearer {bob_token}"},
        "body": json.dumps({"name": "Bob Hijack Attempt"}),
    }
    resp = cp_lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 403
    assert "forbidden" in resp["body"]
    assert store.learned_routines[routine_id]["name"] == "Alice Deep Work"

    # Alice sends request with Alice's JWT
    alice_token = create_test_jwt("usr_alice")
    event_alice = {
        "path": f"/api/v1/routines/{routine_id}",
        "httpMethod": "PUT",
        "headers": {"Authorization": f"Bearer {alice_token}"},
        "body": json.dumps({"name": "Alice Legitimate Update"}),
    }
    resp_alice = cp_lambda_handler(event_alice, store_override=store)
    assert resp_alice["statusCode"] == 200
    assert store.learned_routines[routine_id]["name"] == "Alice Legitimate Update"


def test_different_user_cannot_revoke_routine():
    """Revoking someone else's routine returns 403 Forbidden."""
    store = InMemoryDataStore()
    create_payload = {
        "user_id": "usr_alice",
        "intent": "Reading time",
        "actions": [{"device": "dev_lamp", "action": "warm_light"}],
    }
    create_res = handle_create_routine(create_payload, store=store)
    routine_id = json.loads(create_res["body"])["routine_id"]

    # Bob attempts to revoke Alice's routine
    revoke_res = handle_revoke_routine(routine_id, user_id="usr_bob", store=store)
    assert revoke_res["statusCode"] == 403
    assert "forbidden" in revoke_res["body"]
    assert store.learned_routines[routine_id]["active"] is True

    # Alice legitimately revokes her routine
    alice_revoke_res = handle_revoke_routine(routine_id, user_id="usr_alice", store=store)
    assert alice_revoke_res["statusCode"] == 200
    assert store.learned_routines[routine_id]["active"] is False


# ===========================================================================
# 4. Test-Only Authentication Bypass Hardening Tests
# ===========================================================================

def test_test_bypass_cannot_be_activated_in_live_aws_environment(monkeypatch: pytest.MonkeyPatch):
    """In live AWS environments (AWS_LAMBDA_FUNCTION_NAME set), test bypass is strictly blocked."""
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "AuraControlPlaneFunction")
    monkeypatch.delenv("AURA_STRICT_AUTH", raising=False)

    event = {
        "queryStringParameters": {"user_id": "usr_admin", "tenant_id": "tenant_enterprise"},
        "body": json.dumps({"user_id": "usr_admin", "tenant_id": "tenant_enterprise"}),
        "headers": {},
    }

    # Even with allow_test_bypass=True, live AWS environment blocks bypass
    auth_ctx = extract_and_validate_auth(event, allow_test_bypass=True)
    assert auth_ctx.is_authenticated is False
    assert auth_ctx.user_id == ""
    assert auth_ctx.error == "missing_credentials"

    # Control plane router rejects with 401 Unauthorized
    store = InMemoryDataStore()
    cp_event = {
        "path": "/api/v1/consent/grants",
        "httpMethod": "GET",
        "queryStringParameters": {"user_id": "usr_admin"},
        "headers": {},
    }
    resp = cp_lambda_handler(cp_event, store_override=store)
    assert resp["statusCode"] == 401
    assert "missing_credentials" in resp["body"]
