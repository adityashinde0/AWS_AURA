"""
Unit tests validating the backend contract expected by frontend/app.js.
Ensures that all endpoints called by auraApiClient in app.js have compatible
request parameters and return the expected schema.
"""
from __future__ import annotations

import json
from importlib import import_module
from typing import Any

control_plane = import_module("lambda.control_plane")
InMemoryDataStore = control_plane.InMemoryDataStore


def _parse_body(resp: dict[str, Any]) -> dict[str, Any]:
    return json.loads(resp["body"])


def test_frontend_focus_signal_contract():
    """Validates POST /api/v1/focus/signal with payload sent by frontend triggerDistraction."""
    store = InMemoryDataStore()
    user_id = "usr_marcus"
    session_id = "sess_marcus_test123"

    body = {
        "user_id": user_id,
        "session_id": session_id,
        "purpose": "focus_guardian",
        "explicit_signal": "self_report_distracted",
        "consented_signal_score": 0.80,
        "user_intent": "self_report_distracted",
        "connected_devices": ["dev_firetv_living_room"],
    }

    resp = control_plane.handle_focus_signal(body, store=store)
    assert resp["statusCode"] == 200
    data = _parse_body(resp)

    # Required fields checked by frontend app.js
    assert "proposal" in data
    assert "decision" in data
    assert "undo_token" in data
    assert data["proposal"]["is_distracted"] is True
    assert data["proposal"]["recommended_action"] in ("request_pause", "nudge")


def test_frontend_focus_session_start_and_stop_contract():
    """Validates POST /api/v1/focus/session start and stop contracts."""
    store = InMemoryDataStore()
    user_id = "usr_marcus"
    session_id = "sess_marcus_active_001"

    # 1. Start session
    start_body = {
        "user_id": user_id,
        "action": "start",
        "duration_minutes": 25,
        "session_id": session_id,
    }
    start_resp = control_plane.handle_focus_session(start_body, store=store)
    assert start_resp["statusCode"] == 200
    start_data = _parse_body(start_resp)
    assert start_data["session_id"] == session_id
    assert start_data["status"] == "active"
    assert start_data["duration_minutes"] == 25

    # 2. Stop session
    stop_body = {
        "user_id": user_id,
        "action": "stop",
        "session_id": session_id,
    }
    stop_resp = control_plane.handle_focus_session(stop_body, store=store)
    assert stop_resp["statusCode"] == 200
    stop_data = _parse_body(stop_resp)
    assert stop_data["status"] == "completed"
    assert "ended_at" in stop_data


def test_frontend_consent_grants_crud_contract():
    """Validates GET /api/v1/consent/grants, POST grant, and DELETE grant contracts."""
    store = InMemoryDataStore()
    user_id = "usr_marcus"

    # 1. Get existing grants
    get_resp = control_plane.handle_get_grants(user_id, store=store)
    assert get_resp["statusCode"] == 200
    get_data = _parse_body(get_resp)
    assert "grants" in get_data
    assert isinstance(get_data["grants"], list)

    # 2. Create new grant (HTTP 201 Created)
    create_body = {
        "user_id": user_id,
        "action_type": "request_pause",
        "scope": "allow_session",
        "target_allowlist": ["dev_firetv_living_room"],
        "ttl_seconds": 86400,
    }
    create_resp = control_plane.handle_create_grant(create_body, store=store)
    assert create_resp["statusCode"] == 201
    new_grant = _parse_body(create_resp)
    grant_id = new_grant["grant_id"]
    assert new_grant["action_type"] == "request_pause"
    assert new_grant["active"] is True

    # 3. Revoke grant
    revoke_resp = control_plane.handle_revoke_grant(grant_id, user_id, store=store)
    assert revoke_resp["statusCode"] == 200
    revoked_data = _parse_body(revoke_resp)
    assert revoked_data["revoked"] is True
    assert revoked_data["grant_id"] == grant_id


def test_frontend_audit_records_contract():
    """Validates GET /api/v1/audit/actions contract."""
    store = InMemoryDataStore()
    user_id = "usr_marcus"

    resp = control_plane.handle_query_audit(user_id, limit=10, store=store)
    assert resp["statusCode"] == 200
    data = _parse_body(resp)
    assert "records" in data
    assert isinstance(data["records"], list)


def test_frontend_undo_action_contract():
    """Validates POST /api/v1/actions/undo contract with server-side consent, execution, and replay."""
    store = InMemoryDataStore()
    user_id = "usr_marcus"

    # Step 1: Without consent grant, action is NOT executed (decision = suggested_only, undo_token = None)
    unauthorized_resp = control_plane.handle_focus_signal(
        {
            "user_id": user_id,
            "session_id": "sess_12345678",
            "explicit_signal": "self_report_distracted",
        },
        store=store,
    )
    unauthorized_data = _parse_body(unauthorized_resp)
    assert unauthorized_data["decision"] == "suggested_only"
    assert unauthorized_data["undo_token"] is None

    # Step 2: Grant consent for session
    store.grants["grant_test"] = {
        "grant_id": "grant_test",
        "user_id": user_id,
        "action_type": "request_pause",
        "scope": "allow_session",
        "target_allowlist": ["dev_firetv_living_room"],
        "active": True,
    }

    # Step 3: Trigger action with consent grant active -> executed with undo token
    sig_resp = control_plane.handle_focus_signal(
        {
            "user_id": user_id,
            "session_id": "sess_12345678",
            "explicit_signal": "self_report_distracted",
        },
        store=store,
    )
    sig_data = _parse_body(sig_resp)
    assert sig_data["decision"] == "executed"
    valid_undo_token = sig_data.get("undo_token")
    assert valid_undo_token is not None

    # Step 4: Execute undo
    undo_resp = control_plane.handle_undo_action(
        {"user_id": user_id, "undo_token": valid_undo_token},
        store=store,
    )
    assert undo_resp["statusCode"] == 200
    undo_data = _parse_body(undo_resp)
    assert undo_data["success"] is True
    assert undo_data["device_result"]["success"] is True

    # Step 5: Second undo should fail (token already used / unknown)
    dup_resp = control_plane.handle_undo_action(
        {"user_id": user_id, "undo_token": valid_undo_token},
        store=store,
    )
    assert dup_resp["statusCode"] in (200, 404, 409)
    dup_data = _parse_body(dup_resp)
    assert dup_data.get("idempotent") is True or dup_resp["statusCode"] == 404


def test_frontend_auth_header_and_error_contract():
    """Validates that Authorization header is processed and granular error statuses are returned."""
    # 1. Test missing credentials -> 401
    unauth_resp = control_plane.lambda_handler({
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"user_id": "usr_test"},
    })
    assert unauth_resp["statusCode"] == 401

    # 2. Test cross-user attempt -> 403
    auth_module = import_module("lambda.auth")
    test_token = auth_module.create_test_jwt("usr_caller_1")
    forbidden_resp = control_plane.lambda_handler({
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {test_token}"},
        "queryStringParameters": {"user_id": "usr_target_2"},
    })
    assert forbidden_resp["statusCode"] == 403

