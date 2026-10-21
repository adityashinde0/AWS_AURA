import importlib
import json
import time
import pytest

_cp = importlib.import_module("lambda.control_plane")
InMemoryDataStore = _cp.InMemoryDataStore
handle_create_grant = _cp.handle_create_grant
handle_focus_session = _cp.handle_focus_session
handle_get_grants = _cp.handle_get_grants
handle_query_audit = _cp.handle_query_audit
handle_register_wearable = _cp.handle_register_wearable
handle_revoke_grant = _cp.handle_revoke_grant
handle_safety_escalate = _cp.handle_safety_escalate
handle_safety_cancel = _cp.handle_safety_cancel
handle_undo_action = _cp.handle_undo_action
handle_get_preferences = _cp.handle_get_preferences
handle_update_preferences = _cp.handle_update_preferences
lambda_handler = _cp.lambda_handler


@pytest.fixture
def store() -> InMemoryDataStore:
    return InMemoryDataStore()


def test_create_and_get_consent_grant(store: InMemoryDataStore) -> None:
    body = {
        "user_id": "usr_aditi_01",
        "action_type": "request_pause",
        "scope": "allow_session",
        "target_allowlist": ["firetv_living_room"],
        "ttl_seconds": 3600,
    }
    create_resp = handle_create_grant(body, store=store)
    assert create_resp["statusCode"] == 201
    grant = json.loads(create_resp["body"])
    assert grant["user_id"] == "usr_aditi_01"
    assert grant["active"] is True
    grant_id = grant["grant_id"]

    # Get grants
    get_resp = handle_get_grants("usr_aditi_01", store=store)
    assert get_resp["statusCode"] == 200
    grants = json.loads(get_resp["body"])["grants"]
    assert len(grants) == 1
    assert grants[0]["grant_id"] == grant_id


def test_immediate_revocation(store: InMemoryDataStore) -> None:
    body = {
        "user_id": "usr_aditi_01",
        "action_type": "request_dnd",
        "scope": "allow_session",
    }
    create_resp = handle_create_grant(body, store=store)
    grant_id = json.loads(create_resp["body"])["grant_id"]

    revoke_resp = handle_revoke_grant(grant_id, "usr_aditi_01", store=store)
    assert revoke_resp["statusCode"] == 200
    revoked_body = json.loads(revoke_resp["body"])
    assert revoked_body["revoked"] is True

    # Active grants list is now empty (<60s effective revocation)
    get_resp = handle_get_grants("usr_aditi_01", store=store)
    grants = json.loads(get_resp["body"])["grants"]
    assert len(grants) == 0


def test_focus_session_lifecycle(store: InMemoryDataStore) -> None:
    start_body = {"user_id": "usr_aditi_01", "action": "start", "duration_minutes": 45}
    start_resp = handle_focus_session(start_body, store=store)
    assert start_resp["statusCode"] == 200
    session = json.loads(start_resp["body"])
    assert session["status"] == "active"
    session_id = session["session_id"]

    stop_body = {"user_id": "usr_aditi_01", "action": "stop", "session_id": session_id}
    stop_resp = handle_focus_session(stop_body, store=store)
    assert stop_resp["statusCode"] == 200
    stopped = json.loads(stop_resp["body"])
    assert stopped["status"] == "completed"


def test_undo_action_and_audit(store: InMemoryDataStore) -> None:
    # First record an action
    store.audit_records.append(
        {
            "action_id": "act_101",
            "user_id": "usr_aditi_01",
            "tenant_id": "tenant_default",
            "action_type": "request_pause",
            "target_device": "firetv_01",
            "status": "executed",
            "reason": "Paused video app",
            "undo_token": "token_reversal_123",
            "timestamp": 1791100000.0,
        }
    )

    undo_body = {"user_id": "usr_aditi_01", "tenant_id": "tenant_default", "undo_token": "token_reversal_123"}
    undo_resp = handle_undo_action(undo_body, store=store)
    assert undo_resp["statusCode"] == 200
    undo_data = json.loads(undo_resp["body"])
    assert undo_data["success"] is True

    # Check audit log shows the reversal
    audit_resp = handle_query_audit("usr_aditi_01", store=store)
    records = json.loads(audit_resp["body"])["records"]
    assert len(records) == 2
    assert any(r["status"] == "undone" for r in records)


def test_lambda_handler_http_routes(store: InMemoryDataStore) -> None:
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"user_id": "usr_aditi_01"},
    }
    resp = lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# Phase 7: Wearable registration & Safety escalation tests
# ---------------------------------------------------------------------------

def test_wearable_registration_and_single_active_binding(store: InMemoryDataStore) -> None:
    """First registration succeeds; re-registration with a new device_id revokes the old
    binding (Single Active Watch Binding rule, docs/plans/apple_implementation_plan.md §7.1)."""

    # --- First registration ---
    body_first = {
        "user_id": "usr_wearable_01",
        "device_id": "watch_abc",
        "push_token": "apns_token_first",
        "platform": "watchos",
        "public_key": "fingerprint_abc",
    }
    resp_first = handle_register_wearable(body_first, store=store)
    assert resp_first["statusCode"] == 200
    data_first = json.loads(resp_first["body"])
    assert data_first["registered"] is True
    assert data_first["device_id"] == "watch_abc"
    assert data_first["revoked_previous"] is False

    # Binding is stored and active
    binding = store.wearable_bindings["usr_wearable_01"]
    assert binding["active"] is True
    assert binding["push_token"] == "apns_token_first"

    # --- Second registration with a *different* device_id ---
    body_second = {
        "user_id": "usr_wearable_01",
        "device_id": "watch_xyz",
        "push_token": "apns_token_second",
        "platform": "watchos",
    }
    resp_second = handle_register_wearable(body_second, store=store)
    assert resp_second["statusCode"] == 200
    data_second = json.loads(resp_second["body"])
    assert data_second["registered"] is True
    assert data_second["device_id"] == "watch_xyz"
    assert data_second["revoked_previous"] is True  # old binding revoked

    # Old binding record is now inactive (it was mutated in-place then overwritten)
    new_binding = store.wearable_bindings["usr_wearable_01"]
    assert new_binding["device_id"] == "watch_xyz"
    assert new_binding["active"] is True

    # A security audit record was created
    security_events = [r for r in store.audit_records if r.get("category") == "security"]
    assert len(security_events) == 1
    assert security_events[0]["event"] == "wearable_binding_revoked"
    assert security_events[0]["old_device_id"] == "watch_abc"
    assert security_events[0]["new_device_id"] == "watch_xyz"

    # --- Re-registration with the *same* device_id must NOT revoke ---
    body_same = {
        "user_id": "usr_wearable_01",
        "device_id": "watch_xyz",
        "push_token": "apns_token_refreshed",
        "platform": "watchos",
    }
    resp_same = handle_register_wearable(body_same, store=store)
    assert resp_same["statusCode"] == 200
    data_same = json.loads(resp_same["body"])
    assert data_same["revoked_previous"] is False
    # Still only one security event total
    assert len([r for r in store.audit_records if r.get("category") == "security"]) == 1

    # --- Invalid platform rejected ---
    bad_platform = {
        "user_id": "usr_wearable_01",
        "device_id": "watch_xyz",
        "push_token": "tok",
        "platform": "symbian",
    }
    resp_bad = handle_register_wearable(bad_platform, store=store)
    assert resp_bad["statusCode"] == 400

    # --- Route via lambda_handler for /apns/register ---
    event_apns = {
        "path": "/apns/register",
        "httpMethod": "POST",
        "body": json.dumps(body_same),
    }
    resp_route = lambda_handler(event_apns, store_override=store)
    assert resp_route["statusCode"] == 200

    # --- Route via lambda_handler for /api/v1/devices/wearable ---
    event_v1 = {
        "path": "/api/v1/devices/wearable",
        "httpMethod": "POST",
        "body": json.dumps(body_same),
    }
    resp_v1 = lambda_handler(event_v1, store_override=store)
    assert resp_v1["statusCode"] == 200


def test_safety_escalation_lifecycle_and_consent_check(store: InMemoryDataStore) -> None:
    """Escalation is blocked without a safety_guardian consent grant; succeeds with one."""

    payload = {
        "user_id": "usr_safety_01",
        "incident_id": "inc_001",
        "reason": "manual_sos",
        "location": {"latitude": 37.77, "longitude": -122.42},
        "timestamp": int(time.time()),
    }

    # --- No consent grant → 403 ---
    resp_no_consent = handle_safety_escalate(payload, store=store)
    assert resp_no_consent["statusCode"] == 403
    body_403 = json.loads(resp_no_consent["body"])
    assert body_403["error"] == "consent_required"

    # Seed a safety_guardian consent grant directly (not via handle_create_grant,
    # which restricts action_type to a smaller set)
    store.grants["grant_sg_01"] = {
        "grant_id": "grant_sg_01",
        "user_id": "usr_safety_01",
        "action_type": "safety_guardian",
        "scope": "allow_always",
        "active": True,
    }

    # --- With consent → 200 escalated ---
    resp_ok = handle_safety_escalate(payload, store=store)
    assert resp_ok["statusCode"] == 200
    body_ok = json.loads(resp_ok["body"])
    assert body_ok["status"] == "escalated"
    assert body_ok["incident_id"] == "inc_001"
    assert body_ok["contacts_notified"] is True

    # Privacy-minimised audit record: category + timestamp only; no health/location fields
    esc_records = [r for r in store.audit_records if r.get("category") == "safety_escalation"]
    assert len(esc_records) == 1
    audit_record = esc_records[0]
    assert "location" not in audit_record
    assert "reason" not in audit_record
    assert "timestamp" in audit_record

    # --- Route via lambda_handler ---
    event = {
        "path": "/api/v1/safety/escalate",
        "httpMethod": "POST",
        "body": json.dumps({**payload, "incident_id": "inc_002"}),
    }
    # Reset the rate-limit timestamp so this call is treated as a fresh escalation
    store.escalation_timestamps["usr_safety_01"] = 0.0
    resp_route = lambda_handler(event, store_override=store)
    assert resp_route["statusCode"] == 200


def test_safety_escalation_rate_limiting(store: InMemoryDataStore) -> None:
    """Second escalation within 60 s is idempotent (rate_limited=True); no duplicate audit."""

    store.grants["grant_sg_rl"] = {
        "grant_id": "grant_sg_rl",
        "user_id": "usr_safety_02",
        "action_type": "safety_guardian",
        "scope": "allow_always",
        "active": True,
    }

    payload = {
        "user_id": "usr_safety_02",
        "incident_id": "inc_rl_001",
        "reason": "timeout_no_response",
        "timestamp": int(time.time()),
    }

    # First call — genuine escalation
    resp_first = handle_safety_escalate(payload, store=store)
    assert resp_first["statusCode"] == 200
    data_first = json.loads(resp_first["body"])
    assert data_first.get("rate_limited") is None or data_first.get("rate_limited") is False

    audit_count_after_first = len(
        [r for r in store.audit_records if r.get("category") == "safety_escalation"]
    )
    assert audit_count_after_first == 1

    # Second call immediately after — should be rate-limited
    resp_second = handle_safety_escalate(payload, store=store)
    assert resp_second["statusCode"] == 200
    data_second = json.loads(resp_second["body"])
    assert data_second["status"] == "escalated"
    assert data_second["contacts_notified"] is True
    assert data_second.get("rate_limited") is True

    # No additional audit record written
    audit_count_after_second = len(
        [r for r in store.audit_records if r.get("category") == "safety_escalation"]
    )
    assert audit_count_after_second == 1  # still only one

    # After the window passes, a third call is treated as a fresh escalation
    store.escalation_timestamps["usr_safety_02"] = time.time() - 61  # simulate 61 s ago
    resp_third = handle_safety_escalate(payload, store=store)
    assert resp_third["statusCode"] == 200
    data_third = json.loads(resp_third["body"])
    assert data_third.get("rate_limited") is None or data_third.get("rate_limited") is False
    assert len(
        [r for r in store.audit_records if r.get("category") == "safety_escalation"]
    ) == 2


# ---------------------------------------------------------------------------
# Sub-Task 3: Notification Feedback API tests
# ---------------------------------------------------------------------------

handle_notification_feedback = _cp.handle_notification_feedback


class TestNotificationFeedback:
    """Unit tests for handle_notification_feedback() — Sub-Task 3."""

    def _body(self, action: str = "Read", **overrides) -> dict:
        base = {
            "user_id": "usr_fb_01",
            "card_id": "card_abc123",
            "calendar_event_id": "cal_evt_001",
            "action": action,
        }
        base.update(overrides)
        return base

    def test_read_action_records_feedback(self) -> None:
        store = InMemoryDataStore()
        resp = handle_notification_feedback(self._body("Read"), store=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["ok"] is True
        assert data["action"] == "Read"

    def test_save_action_records_feedback(self) -> None:
        store = InMemoryDataStore()
        resp = handle_notification_feedback(self._body("Save"), store=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["ok"] is True

    def test_not_useful_action_records_feedback(self) -> None:
        store = InMemoryDataStore()
        resp = handle_notification_feedback(self._body("Not useful"), store=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["ok"] is True
        assert data["action"] == "Not useful"

    def test_mute_series_updates_muted_series(self) -> None:
        store = InMemoryDataStore()
        body = self._body("Mute this event series", series_id="series_standup_01")
        resp = handle_notification_feedback(body, store=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["ok"] is True
        assert data["muted_series_count"] == 1
        # Preference is stored in the store
        prefs = store.whisper_preferences.get("usr_fb_01", {})
        assert "series_standup_01" in prefs.get("muted_series", [])

    def test_mute_series_without_series_id_still_succeeds(self) -> None:
        """series_id is optional; mute without it records the feedback but doesn't add to muted_series."""
        store = InMemoryDataStore()
        body = self._body("Mute this event series")  # no series_id
        resp = handle_notification_feedback(body, store=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["ok"] is True
        assert data["muted_series_count"] == 0

    def test_mute_series_idempotent(self) -> None:
        """Muting the same series twice doesn't duplicate the entry."""
        store = InMemoryDataStore()
        body = self._body("Mute this event series", series_id="series_dup_01")
        handle_notification_feedback(body, store=store)
        handle_notification_feedback(body, store=store)
        prefs = store.whisper_preferences.get("usr_fb_01", {})
        assert prefs["muted_series"].count("series_dup_01") == 1

    def test_feedback_appends_audit_record(self) -> None:
        store = InMemoryDataStore()
        resp = handle_notification_feedback(self._body("Save"), store=store)
        data = json.loads(resp["body"])
        audit = [r for r in store.audit_records if r.get("action_type") == "notification_feedback"]
        assert len(audit) == 1
        assert audit[0]["user_id"] == "usr_fb_01"
        assert audit[0]["card_id"] == "card_abc123"
        assert audit[0]["action"] == "Save"
        assert audit[0]["action_id"] == data["audit_id"]

    def test_missing_required_fields_returns_400(self) -> None:
        store = InMemoryDataStore()
        for missing in ("user_id", "card_id", "calendar_event_id", "action"):
            body = self._body("Read")
            del body[missing]
            resp = handle_notification_feedback(body, store=store)
            assert resp["statusCode"] == 400

    def test_invalid_action_returns_400(self) -> None:
        store = InMemoryDataStore()
        resp = handle_notification_feedback(self._body("Dismiss"), store=store)
        assert resp["statusCode"] == 400
        data = json.loads(resp["body"])
        assert data["error"] == "invalid_enum"

    def test_route_via_lambda_handler(self) -> None:
        store = InMemoryDataStore()
        event = {
            "path": "/api/v1/notifications/feedback",
            "httpMethod": "POST",
            "body": json.dumps(self._body("Read")),
        }
        resp = lambda_handler(event, store_override=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["ok"] is True


def test_safety_escalation_cancelled_within_window(store: InMemoryDataStore) -> None:
    """handle_safety_cancel within 60s window prevents the escalation send."""
    store.grants["grant_sg_cancel"] = {
        "grant_id": "grant_sg_cancel",
        "user_id": "usr_safety_cancel",
        "action_type": "safety_guardian",
        "scope": "allow_always",
        "active": True,
    }

    # User cancels check-in within window
    cancel_resp = handle_safety_cancel({"user_id": "usr_safety_cancel", "incident_id": "inc_cancel_01"}, store=store)
    assert cancel_resp["statusCode"] == 200
    cancel_data = json.loads(cancel_resp["body"])
    assert cancel_data["status"] == "cancelled"
    assert cancel_data["ok"] is True

    # Audit trail has escalation_cancelled
    cancel_audits = [r for r in store.audit_records if r.get("category") == "escalation_cancelled"]
    assert len(cancel_audits) == 1
    assert cancel_audits[0]["user_id"] == "usr_safety_cancel"

    # Subsequent escalation call is aborted / prevented
    esc_resp = handle_safety_escalate({
        "user_id": "usr_safety_cancel",
        "incident_id": "inc_cancel_01",
        "reason": "timeout",
    }, store=store)
    assert esc_resp["statusCode"] == 200
    esc_data = json.loads(esc_resp["body"])
    assert esc_data["status"] == "cancelled"
    assert esc_data["contacts_notified"] is False

    # No safety_escalation audit record was appended
    esc_audits = [r for r in store.audit_records if r.get("category") == "safety_escalation"]
    assert len(esc_audits) == 0


def test_safety_escalation_provider_error_records_failed_audit(store: InMemoryDataStore) -> None:
    """If SMS adapter fails, status is escalation_failed and audit is recorded."""
    store.grants["grant_sg_err"] = {
        "grant_id": "grant_sg_err",
        "user_id": "usr_safety_err",
        "action_type": "safety_guardian",
        "scope": "allow_always",
        "active": True,
    }

    class FailingSmsAdapter:
        def send_sms(self, phone: str, msg: str):
            raise RuntimeError("Provider network timeout")

    store.sms_adapter = FailingSmsAdapter()

    resp = handle_safety_escalate({
        "user_id": "usr_safety_err",
        "incident_id": "inc_err_01",
        "reason": "manual_sos",
        "contact_phone": "+15559998888",
    }, store=store)
    assert resp["statusCode"] == 502
    data = json.loads(resp["body"])
    assert data["status"] == "escalation_failed"
    assert data["contacts_notified"] is False

    # Audit record has escalation_failed
    failed_audits = [r for r in store.audit_records if r.get("status") == "escalation_failed"]
    assert len(failed_audits) == 1
    assert failed_audits[0]["category"] == "safety_escalation"


def test_safety_cancel_route_via_lambda_handler(store: InMemoryDataStore) -> None:
    event = {
        "path": "/api/v1/safety/cancel",
        "httpMethod": "POST",
        "body": json.dumps({"user_id": "usr_safety_route", "incident_id": "inc_route_01"}),
    }
    resp = lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])
    assert data["status"] == "cancelled"


class TestUserPreferences:
    def test_get_preferences_returns_conservative_defaults_for_new_user(self, store: InMemoryDataStore) -> None:
        resp = handle_get_preferences("usr_pref_new", store=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        prefs = data["preferences"]
        assert prefs["pre_meeting_enabled"] is False
        assert prefs["notification_preview"] == "generic"
        assert prefs["focus_action_scope"] == "ask_every_time"
        assert prefs["slack_status_enabled"] is False
        assert prefs["safety_guardian_enabled"] is False
        assert prefs["location_in_sos"] == "ask_each_time"
        assert "contacts" not in prefs
        assert "phone_numbers" not in prefs

    def test_update_preferences_valid_patch_and_audit(self, store: InMemoryDataStore) -> None:
        patch = {
            "pre_meeting_enabled": True,
            "notification_preview": "private_context",
            "focus_action_scope": "this_session",
        }
        resp = handle_update_preferences({"user_id": "usr_pref_01", "preferences": patch}, store=store)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["ok"] is True
        assert data["preferences"]["pre_meeting_enabled"] is True
        assert data["preferences"]["notification_preview"] == "private_context"

        # Verify audit record appended
        audits = [r for r in store.audit_records if r.get("category") == "preferences_updated"]
        assert len(audits) == 1
        assert audits[0]["user_id"] == "usr_pref_01"

    def test_update_preferences_rejects_unknown_keys(self, store: InMemoryDataStore) -> None:
        patch = {"unknown_key": "some_value"}
        resp = handle_update_preferences({"user_id": "usr_pref_02", "preferences": patch}, store=store)
        assert resp["statusCode"] == 400
        data = json.loads(resp["body"])
        assert data["error"] == "unknown_preference_key"

    def test_update_preferences_rejects_invalid_enums(self, store: InMemoryDataStore) -> None:
        patch = {"notification_preview": "invalid_mode"}
        resp = handle_update_preferences({"user_id": "usr_pref_03", "preferences": patch}, store=store)
        assert resp["statusCode"] == 400
        data = json.loads(resp["body"])
        assert data["error"] == "invalid_enum"

    def test_preferences_routes_via_lambda_handler(self, store: InMemoryDataStore) -> None:
        # GET route
        get_evt = {
            "path": "/api/v1/preferences",
            "httpMethod": "GET",
            "queryStringParameters": {"user_id": "usr_pref_route"},
        }
        get_resp = lambda_handler(get_evt, store_override=store)
        assert get_resp["statusCode"] == 200

        # PATCH route
        patch_evt = {
            "path": "/api/v1/preferences",
            "httpMethod": "PATCH",
            "body": json.dumps({"user_id": "usr_pref_route", "preferences": {"pre_meeting_enabled": True}}),
        }
        patch_resp = lambda_handler(patch_evt, store_override=store)
        assert patch_resp["statusCode"] == 200
        data = json.loads(patch_resp["body"])
        assert data["preferences"]["pre_meeting_enabled"] is True
