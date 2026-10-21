import importlib
import json
import time
from unittest.mock import MagicMock
import pytest

_ws = importlib.import_module("lambda.websocket_handler")
handle_connect = _ws.handle_connect
handle_disconnect = _ws.handle_disconnect
handle_relay = _ws.handle_relay
lambda_handler = _ws.lambda_handler
validate_connect_token = _ws.validate_connect_token

_auth = importlib.import_module("lambda.auth")


def test_validate_connect_token() -> None:
    # Short token fails
    valid, _ = validate_connect_token("short")
    assert not valid

    # Empty token fails
    valid, _ = validate_connect_token("")
    assert not valid

    # Forged / tampered token fails
    tampered_token = _auth.create_test_jwt("usr_12345678", tamper_signature=True)
    valid, _ = validate_connect_token(f"Bearer {tampered_token}")
    assert not valid

    # Valid RSA-signed token succeeds
    valid_token = _auth.create_test_jwt("usr_12345678")
    valid, claims = validate_connect_token(f"Bearer {valid_token}")
    assert valid
    assert claims["user_id"] == "usr_12345678"
    assert claims["tenant_id"] == "tenant_default"


def test_handle_connect_success() -> None:
    mock_table = MagicMock()
    token = _auth.create_test_jwt("usr_aditi_01", tenant_id="tenant_default")
    event = {
        "requestContext": {"connectionId": "conn_12345"},
        "queryStringParameters": {
            "auth_token": token,
            "user_id": "usr_aditi_01",
        },
    }
    resp = handle_connect(event, table=mock_table)
    assert resp["statusCode"] == 200
    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args[1]["Item"]
    assert item["connectionId"] == "conn_12345"
    assert item["userId"] == "usr_aditi_01"
    assert item["tenantId"] == "tenant_default"


def test_handle_connect_uses_trusted_authorizer_claims() -> None:
    """When API Gateway pre-validates claims in requestContext.authorizer, prefer them."""
    mock_table = MagicMock()
    event = {
        "requestContext": {
            "connectionId": "conn_auth_ctx",
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "usr_authorizer_verified",
                        "custom:tenant_id": "tenant_verified",
                    }
                }
            },
        },
    }
    resp = handle_connect(event, table=mock_table)
    assert resp["statusCode"] == 200
    mock_table.put_item.assert_called_once()
    item = mock_table.put_item.call_args[1]["Item"]
    assert item["userId"] == "usr_authorizer_verified"
    assert item["tenantId"] == "tenant_verified"


def test_handle_connect_rejects_user_spoofing() -> None:
    mock_table = MagicMock()
    token = _auth.create_test_jwt("usr_real_caller")
    # Caller attempts to bind to usr_victim
    event = {
        "requestContext": {"connectionId": "conn_12345"},
        "queryStringParameters": {
            "auth_token": token,
            "user_id": "usr_victim",
        },
    }
    resp = handle_connect(event, table=mock_table)
    assert resp["statusCode"] == 403
    body = json.loads(resp["body"])
    assert body["error"] == "forbidden"
    mock_table.put_item.assert_not_called()


def test_handle_connect_unauthorized() -> None:
    mock_table = MagicMock()
    event = {
        "requestContext": {"connectionId": "conn_12345"},
        "queryStringParameters": {"auth_token": "bad"},
    }
    resp = handle_connect(event, table=mock_table)
    assert resp["statusCode"] == 403
    mock_table.put_item.assert_not_called()


def test_handle_disconnect() -> None:
    mock_table = MagicMock()
    event = {"requestContext": {"connectionId": "conn_12345"}}
    resp = handle_disconnect(event, table=mock_table)
    assert resp["statusCode"] == 200
    mock_table.delete_item.assert_called_once_with(Key={"connectionId": "conn_12345"})


def test_handle_relay_success_returns_ack() -> None:
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_001",
        "event_type": "focus_signal",
        "user_id": "usr_aditi_01",
        "timestamp": 1791100000.0,
        "evidence": [{"id": "evt_001", "snippet": "AWS session"}],
        "signal_payload": {"explicit_signal": "self_report_distracted"},
    }
    resp = handle_relay(body, bus_client=mock_bus, bus_name="aura-event-bus", authenticated_user_id="usr_aditi_01")
    assert resp["statusCode"] == 200
    ack = resp["body"]
    assert ack["status"] == "accepted"
    assert ack["event_id"] == "evt_001"
    assert "ack_id" in ack
    mock_bus.put_events.assert_called_once()


def test_handle_relay_unauthenticated_fails_closed() -> None:
    """Relay strictly fails closed with 403 whenever authenticated identity is absent."""
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_unauth_01",
        "event_type": "focus_signal",
        "user_id": "usr_aditi_01",
        "timestamp": time.time(),
    }
    # No authenticated_user_id passed
    resp = handle_relay(body, bus_client=mock_bus)
    assert resp["statusCode"] == 403
    assert resp["body"]["error"] == "forbidden"
    mock_bus.put_events.assert_not_called()


def test_handle_relay_rejects_impersonation() -> None:
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_spoofed_01",
        "event_type": "focus_signal",
        "user_id": "usr_victim",
        "timestamp": 1791100000.0,
    }
    # Connection is bound to usr_attacker, but body user_id claims to be usr_victim
    resp = handle_relay(body, bus_client=mock_bus, authenticated_user_id="usr_attacker")
    assert resp["statusCode"] == 403
    assert resp["body"]["error"] == "forbidden"
    mock_bus.put_events.assert_not_called()


def test_handle_relay_rejects_prohibited_raw_audio() -> None:
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_bad_01",
        "event_type": "focus_signal",
        "user_id": "usr_aditi_01",
        "raw_audio": "base64_audio_data_should_never_be_here",
    }
    resp = handle_relay(body, bus_client=mock_bus, authenticated_user_id="usr_aditi_01")
    assert resp["statusCode"] == 400
    assert resp["body"]["error"] == "privacy_violation"
    mock_bus.put_events.assert_not_called()


def test_lambda_handler_websocket_route() -> None:
    mock_table = MagicMock()
    token = _auth.create_test_jwt("usr_testuser")
    event = {
        "requestContext": {"routeKey": "$connect", "connectionId": "c_99"},
        "headers": {"Authorization": f"Bearer {token}"},
    }
    res = lambda_handler(event, table_override=mock_table)
    assert res["statusCode"] == 200


def test_lambda_handler_relay_enforces_connection_binding() -> None:
    mock_table = MagicMock()
    mock_bus = MagicMock()
    # Mock connection stored in DynamoDB
    mock_table.get_item.return_value = {
        "Item": {
            "connectionId": "c_99",
            "userId": "usr_alice",
            "tenantId": "tenant_default",
        }
    }

    # 1. Matching user succeeds
    valid_event = {
        "requestContext": {"routeKey": "relay", "connectionId": "c_99"},
        "body": json.dumps({
            "action": "relay",
            "event_id": "evt_101",
            "event_type": "focus_signal",
            "user_id": "usr_alice",
            "timestamp": time.time(),
        }),
    }
    res = lambda_handler(valid_event, table_override=mock_table, bus_override=mock_bus)
    assert res["statusCode"] == 200

    # 2. Impersonated user rejected with 403
    spoofed_event = {
        "requestContext": {"routeKey": "relay", "connectionId": "c_99"},
        "body": json.dumps({
            "action": "relay",
            "event_id": "evt_102",
            "event_type": "focus_signal",
            "user_id": "usr_bob",
            "timestamp": time.time(),
        }),
    }
    res = lambda_handler(spoofed_event, table_override=mock_table, bus_override=mock_bus)
    assert res["statusCode"] == 403
    body = json.loads(res["body"])
    assert body["error"] == "forbidden"

    # 3. Non-existent / unauthenticated connection record rejected with 403
    mock_table.get_item.return_value = {}  # Connection not found
    res = lambda_handler(valid_event, table_override=mock_table, bus_override=mock_bus)
    assert res["statusCode"] == 403


def test_lambda_handler_relay_missing_connection_id_fails_closed() -> None:
    """Missing connectionId in requestContext must fail closed with 403."""
    mock_table = MagicMock()
    mock_bus = MagicMock()
    event = {
        "requestContext": {"routeKey": "relay"},
        "body": json.dumps({
            "action": "relay",
            "event_id": "evt_no_conn",
            "event_type": "focus_signal",
            "user_id": "usr_alice",
            "timestamp": time.time(),
        }),
    }
    res = lambda_handler(event, table_override=mock_table, bus_override=mock_bus)
    assert res["statusCode"] == 403
    body = json.loads(res["body"])
    assert body["error"] == "forbidden"
    mock_bus.put_events.assert_not_called()


def test_lambda_handler_relay_unavailable_storage_fails_closed() -> None:
    """Unavailable connection storage must fail closed with 403, not fall back to untrusted user_id."""
    mock_bus = MagicMock()
    event = {
        "requestContext": {"routeKey": "relay", "connectionId": "c_no_table"},
        "body": json.dumps({
            "action": "relay",
            "event_id": "evt_no_storage",
            "event_type": "focus_signal",
            "user_id": "usr_attacker",
            "timestamp": time.time(),
        }),
    }
    # Pass table_override=None and ensure _dynamodb is None
    res = lambda_handler(event, table_override=None, bus_override=mock_bus)
    assert res["statusCode"] == 403
    body = json.loads(res["body"])
    assert body["error"] == "forbidden"
    mock_bus.put_events.assert_not_called()


def test_validate_connect_token_expired() -> None:
    token = _auth.create_test_jwt("usr_testuser", expires_in_seconds=-100)
    valid, _ = validate_connect_token(f"Bearer {token}")
    assert valid is False


def test_handle_relay_rejects_oversized_payload() -> None:
    mock_bus = MagicMock()
    body = {
        "action": "relay",
        "event_id": "evt_huge_01",
        "event_type": "focus_signal",
        "user_id": "usr_aditi_01",
        "bloat": "x" * 40000,  # 40KB > 32KB
    }
    resp = handle_relay(body, bus_client=mock_bus, authenticated_user_id="usr_aditi_01")
    assert resp["statusCode"] == 400
    assert resp["body"]["error"] == "oversized_payload"
    mock_bus.put_events.assert_not_called()
