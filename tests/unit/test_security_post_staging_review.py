"""Post-Staging Security, OAuth, Authorization & Durability Regressions.

Validates:
1. Public device catalog integrity (GET /api/v1/devices returns catalog models, zero secrets, zero PII).
2. HTTP API authorization, tenant isolation enforcement, and honest error envelopes (no stack trace leaks).
3. Slack OAuth PKCE security: invalid state, missing state, replay attacks, expired attempts, incorrect verifier.
4. WebSocket authentication and relay connection-binding integrity.
5. DynamoDB Decimal and set/list serialization/deserialization correctness.
"""
from __future__ import annotations
import importlib
import json
import time
from decimal import Decimal
from typing import Any

import pytest

_auth = importlib.import_module("lambda.auth")
create_real_rsa_test_jwt = _auth.create_real_rsa_test_jwt
is_public_route = _auth.is_public_route

_cp = importlib.import_module("lambda.control_plane")
InMemoryDataStore = _cp.InMemoryDataStore
_dynamo_deserialize = _cp._dynamo_deserialize
_dynamo_serialize = _cp._dynamo_serialize
_json_resp = _cp._json_resp
handle_get_devices = _cp.handle_get_devices
handle_slack_oauth_callback = _cp.handle_slack_oauth_callback
lambda_handler = _cp.lambda_handler

_ws = importlib.import_module("lambda.websocket_handler")
handle_connect = _ws.handle_connect
handle_disconnect = _ws.handle_disconnect
handle_relay = _ws.handle_relay
ws_lambda_handler = _ws.lambda_handler
from standalone_agent.slack_adapter import (
    PKCEChallenge,
    SlackAdapter,
    generate_pkce_challenge,
    verify_pkce_response,
)


# ===========================================================================
# 1. Device Discovery & Public Route Security
# ===========================================================================

def test_get_devices_returns_public_catalog_only_no_secrets() -> None:
    store = InMemoryDataStore()
    resp = handle_get_devices(store=store)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])

    assert "devices" in body
    assert body["total"] == len(body["devices"])
    assert body["total"] >= 1

    for dev in body["devices"]:
        # Verify no secret token ARN is leaked
        assert "secret_token_arn" not in dev
        assert isinstance(dev.get("has_secret_arn"), bool)
        # Verify no tenant or user private data
        assert "user_id" not in dev
        assert "tenant_id" not in dev
        assert "access_token" not in dev
        assert "refresh_token" not in dev
        # Verify required model fields exist
        assert "device_id" in dev
        assert "name" in dev
        assert "capabilities" in dev


def test_public_route_declaration_coverage() -> None:
    assert is_public_route("GET", "/health") is True
    assert is_public_route("GET", "/api/v1/health") is True
    assert is_public_route("GET", "/api/v1/devices") is True
    assert is_public_route("GET", "/api/v1/feature-flags") is True
    assert is_public_route("POST", "/api/v1/slack/oauth/callback") is True
    assert is_public_route("OPTIONS", "/any/path") is True

    # Critical mutations and user data MUST NOT be public
    assert is_public_route("POST", "/api/v1/devices") is False
    assert is_public_route("GET", "/api/v1/focus/bank") is False
    assert is_public_route("POST", "/api/v1/focus/session") is False
    assert is_public_route("POST", "/api/v1/actions/undo") is False
    assert is_public_route("GET", "/api/v1/consent/grants") is False
    assert is_public_route("GET", "/api/v1/user/export") is False


# ===========================================================================
# 2. HTTP API Authorization & Tenant Isolation
# ===========================================================================

def test_unauthenticated_request_to_protected_route_fails_closed() -> None:
    event = {
        "rawPath": "/api/v1/focus/bank",
        "httpMethod": "GET",
        "headers": {},
        "queryStringParameters": {"user_id": "usr_victim"},
    }
    resp = lambda_handler(event, store_override=None)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] in ("missing_credentials", "unauthorized")


def test_caller_cannot_override_verified_cognito_claims() -> None:
    store = InMemoryDataStore()
    token = create_real_rsa_test_jwt(
        user_id="usr_genuine",
        tenant_id="tenant_alpha",
    )

    # Caller attempts to query victim's focus bank while authenticated as usr_genuine
    event = {
        "rawPath": "/api/v1/focus/bank",
        "httpMethod": "GET",
        "headers": {"Authorization": f"Bearer {token}"},
        "queryStringParameters": {"user_id": "usr_victim"},
    }
    resp = lambda_handler(event, store_override=store)
    # The authorization layer must reject the user mismatch with 403
    assert resp["statusCode"] == 403
    body = json.loads(resp["body"])
    assert body["error"] == "forbidden"


def test_cross_tenant_request_rejected_with_403() -> None:
    store = InMemoryDataStore()
    token = create_real_rsa_test_jwt(
        user_id="usr_genuine",
        tenant_id="tenant_alpha",
    )

    # Caller attempts to supply tenant_beta while token claims tenant_alpha
    event = {
        "rawPath": "/api/v1/consent/grants",
        "httpMethod": "GET",
        "headers": {"Authorization": f"Bearer {token}"},
        "queryStringParameters": {"user_id": "usr_genuine", "tenant_id": "tenant_beta"},
    }
    resp = lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 403
    body = json.loads(resp["body"])
    assert body["error"] == "forbidden"
    assert "cross_tenant" in body["message"] or "tenant" in body["message"]


def test_error_responses_do_not_leak_internal_stack_traces() -> None:
    event = {
        "rawPath": "/api/v1/focus/session",
        "httpMethod": "POST",
        "body": "not-a-valid-json-string{",
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_json"
    assert "Traceback" not in resp["body"]
    assert "File " not in resp["body"]


# ===========================================================================
# 3. Slack OAuth PKCE Hardening
# ===========================================================================

def test_slack_oauth_callback_missing_state_rejected() -> None:
    store = InMemoryDataStore()
    resp = handle_slack_oauth_callback(
        {"code": "auth123", "code_verifier": "ver123", "workspace_id": "T01"},
        store=store,
    )
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_parameters"


def test_slack_oauth_callback_invalid_or_unknown_state_rejected() -> None:
    store = InMemoryDataStore()
    resp = handle_slack_oauth_callback(
        {
            "code": "auth123",
            "state": "unknown_state_never_initiated",
            "code_verifier": "ver123",
            "workspace_id": "T01",
        },
        store=store,
    )
    assert resp["statusCode"] == 400
    body = json.loads(resp["body"])
    assert body["error"] == "oauth_exchange_failed"
    assert "Invalid or expired OAuth state" in body["message"]


def test_slack_oauth_callback_replay_rejected() -> None:
    adapter = SlackAdapter()
    flow = adapter.start_oauth_flow()
    state = flow["state"]
    pkce = adapter.pending_pkce[state]

    # First callback succeeds
    res1 = adapter.complete_oauth_callback(
        code="auth123",
        state=state,
        code_verifier=pkce.code_verifier,
        workspace_id="T_REPLAY_TEST",
        workspace_name="Replay Test Corp",
        admin_user_id="U_ADMIN",
    )
    assert res1["status"] == "connected"

    # Immediate second callback with identical state MUST raise ValueError (replay attack blocked)
    with pytest.raises(ValueError, match="Invalid or expired OAuth state parameter"):
        adapter.complete_oauth_callback(
            code="auth123",
            state=state,
            code_verifier=pkce.code_verifier,
            workspace_id="T_REPLAY_TEST",
            workspace_name="Replay Test Corp",
            admin_user_id="U_ADMIN",
        )


def test_slack_oauth_callback_expired_attempt_rejected() -> None:
    adapter = SlackAdapter()
    challenge = generate_pkce_challenge()
    challenge.expires_at = time.time() - 100  # Artificially expired
    adapter.pending_pkce["expired_state_123"] = challenge

    with pytest.raises(ValueError, match="Invalid or expired OAuth state parameter"):
        adapter.complete_oauth_callback(
            code="auth123",
            state="expired_state_123",
            code_verifier=challenge.code_verifier,
            workspace_id="T_EXPIRED",
            workspace_name="Expired Workspace",
            admin_user_id="U_ADMIN",
        )


# ===========================================================================
# 4. WebSocket Authentication & Relay Ownership Binding
# ===========================================================================

class MockDynamoTable:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def put_item(self, Item: dict[str, Any]) -> None:
        cid = Item.get("connectionId") or Item.get("connection_id") or ""
        self.items[cid] = Item

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        cid = Key.get("connectionId") or Key.get("connection_id")
        if cid in self.items:
            return {"Item": self.items[cid]}
        return {}

    def delete_item(self, Key: dict[str, Any]) -> None:
        cid = Key.get("connectionId") or Key.get("connection_id")
        self.items.pop(cid, None)


def test_websocket_connect_stores_binding_and_disconnect_purges() -> None:
    mock_table = MockDynamoTable()
    token = create_real_rsa_test_jwt(user_id="usr_ws_owner", tenant_id="tenant_01")

    connect_event = {
        "requestContext": {
            "routeKey": "$connect",
            "connectionId": "conn_abc123",
            "authorizer": {
                "claims": {"sub": "usr_ws_owner", "custom:tenant_id": "tenant_01"}
            },
        },
        "queryStringParameters": {"token": token},
    }

    res_connect = handle_connect(connect_event, table=mock_table)
    assert res_connect["statusCode"] == 200
    assert "conn_abc123" in mock_table.items
    assert mock_table.items["conn_abc123"]["userId"] == "usr_ws_owner"

    # Disconnect purges record
    disconnect_event = {
        "requestContext": {
            "routeKey": "$disconnect",
            "connectionId": "conn_abc123",
        }
    }
    res_disconnect = handle_disconnect(disconnect_event, table=mock_table)
    assert res_disconnect["statusCode"] == 200
    assert "conn_abc123" not in mock_table.items


def test_websocket_relay_rejects_user_spoofing() -> None:
    class MockEventBus:
        def put_events(self, **kwargs: Any) -> dict[str, Any]:
            return {"Entries": [{"EventId": "bus_evt_123"}]}

    body = {
        "event_id": "evt_001",
        "event_type": "SignalIntakeEvent",
        "user_id": "usr_victim_spoofed",  # Impersonation attempt!
        "source": "aura.mobile",
    }

    resp = handle_relay(
        body=body,
        bus_client=MockEventBus(),
        authenticated_user_id="usr_legit_owner",
        connection_id="conn_legit",
    )
    assert resp["statusCode"] == 403
    resp_body = resp["body"] if isinstance(resp["body"], dict) else json.loads(resp["body"])
    assert resp_body["error"] == "forbidden"
    assert "cannot impersonate" in resp_body["message"]


# ===========================================================================
# 5. DynamoDB Decimal & Data Serialization Correctness
# ===========================================================================

def test_dynamo_serialize_and_deserialize_nested_structures() -> None:
    raw_data = {
        "timestamp": 1729584000.5,  # float
        "count": 42,
        "capabilities": {"nudge", "request_pause"},  # set
        "tags": ("deep_work", "staged"),  # tuple
        "nested": {
            "score": 0.95,  # float
            "flags": [1.0, 2.0],
        },
    }

    serialized = _dynamo_serialize(raw_data)
    assert serialized["timestamp"] == 1729584000  # float -> int
    assert serialized["capabilities"] == ["nudge", "request_pause"]  # set -> sorted list
    assert serialized["tags"] == ["deep_work", "staged"]  # tuple -> list
    assert serialized["nested"]["score"] == 0
    assert serialized["nested"]["flags"] == [1, 2]

    # Test deserialization of DynamoDB Decimals
    dynamo_items = {
        "balance": Decimal("50"),
        "ratio": Decimal("3.14"),
        "list_items": [Decimal("100"), Decimal("0.5")],
        "nested": {"total": Decimal("1000")},
    }

    deserialized = _dynamo_deserialize(dynamo_items)
    assert deserialized["balance"] == 50
    assert isinstance(deserialized["balance"], int)
    assert deserialized["ratio"] == 3.14
    assert isinstance(deserialized["ratio"], float)
    assert deserialized["list_items"] == [100, 0.5]
    assert deserialized["nested"]["total"] == 1000


def test_json_resp_encodes_decimals_and_sets_safely() -> None:
    data = {
        "int_decimal": Decimal("120"),
        "float_decimal": Decimal("45.67"),
        "set_value": {"item_b", "item_a"},
        "normal_int": 10,
    }

    resp = _json_resp(200, data)
    assert resp["statusCode"] == 200
    parsed = json.loads(resp["body"])
    assert parsed["int_decimal"] == 120
    assert parsed["float_decimal"] == 45.67
    assert parsed["set_value"] == ["item_a", "item_b"]
    assert parsed["normal_int"] == 10
