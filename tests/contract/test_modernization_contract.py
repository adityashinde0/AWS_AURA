"""Contract tests for modernized features in focus_guardian and websocket_handler.

Verifies:
1. Structured logging and fallback_reason tags emitted by focus_guardian when
   encountering bad inputs ("not_implemented", "hallucinated_evidence",
   "validation_error").
2. websocket_handler attaches a correlation_id on every relay response and emits
   a structured WARNING when a token arrives via query-string instead of the
   Authorization header.
"""
from __future__ import annotations

import importlib
import json
import sys
from io import StringIO
from typing import Any
from unittest.mock import MagicMock

import pytest

fg = importlib.import_module("lambda.focus_guardian")
ws = importlib.import_module("lambda.websocket_handler")
auth_mod = importlib.import_module("lambda.auth")

lambda_handler_fg = fg.lambda_handler
lambda_handler_ws = ws.lambda_handler
handle_connect = ws.handle_connect
handle_relay = ws.handle_relay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def capture_logs(monkeypatch: pytest.MonkeyPatch, module: Any):
    """Redirect the module's _log output to a list of parsed dicts."""
    captured: list[dict] = []
    buf = StringIO()

    original_print = print

    def patched_print(*args, **kwargs):
        # Only intercept stdout flushes from the module's _log
        file = kwargs.get("file", sys.stdout)
        if file is sys.stdout:
            text = " ".join(str(a) for a in args)
            try:
                captured.append(json.loads(text))
            except json.JSONDecodeError:
                pass  # non-JSON lines (unlikely) are ignored
        else:
            original_print(*args, **kwargs)

    monkeypatch.setattr(module, "print", patched_print, raising=False)
    # Both modules import builtins.print at call time; patch at the module level
    import builtins
    monkeypatch.setattr(builtins, "print", patched_print)
    return captured


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_fg_event():
    return {
        "user_id": "usr_contract_01",
        "session_id": "sess_contract_00000001",
        "purpose": "focus_guardian",
        "explicit_signal": "none",
        "evidence": [{"id": "evt_c01", "snippet": "Cloud architecture review"}],
        "consented_signal_score": 0.3,
    }


@pytest.fixture
def valid_relay_body():
    return {
        "action": "relay",
        "event_id": "evt_relay_contract_01",
        "event_type": "focus_signal",
        "user_id": "usr_contract_01",
        "session_id": "sess_contract_00000001",
        "evidence": [{"id": "evt_c01", "snippet": "AWS session"}],
        "signal_payload": {"explicit_signal": "none", "consented_signal_score": 0.2},
    }


# ===========================================================================
# Focus Guardian — structured logging & fallback_reason contract
# ===========================================================================

class TestFocusGuardianFallbackReasonContract:
    """Verify every fallback path emits exactly the right fallback_reason tag."""

    def test_not_implemented_fallback_reason_logged(
        self, monkeypatch: pytest.MonkeyPatch, valid_fg_event
    ):
        """No invoker → NotImplementedError → fallback_reason == 'not_implemented'."""
        logs = capture_logs(monkeypatch, fg)

        resp = lambda_handler_fg(valid_fg_event)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["proposal"]["evidence_ids"] == []

        fallback_logs = [
            l for l in logs
            if l.get("event") == "focus_guardian_safe_fallback_activated"
        ]
        assert fallback_logs, "Expected a safe_fallback log record"
        assert fallback_logs[0]["fallback_reason"] == "not_implemented"

    def test_hallucinated_evidence_fallback_reason_logged(
        self, monkeypatch: pytest.MonkeyPatch, valid_fg_event
    ):
        """Model cites an ID not in the request → fallback_reason == 'hallucinated_evidence'."""
        logs = capture_logs(monkeypatch, fg)

        def hallucinating_invoker(_prompt: str) -> dict:
            return {
                "prep_summary": "Fabricated summary.",
                "3_questions": ["Q1?", "Q2?", "Q3?"],
                "emotion_tone": "neutral",
                "is_distracted": False,
                "distraction_score": 0.0,
                "deep_work_bank_delta": 0,
                "evidence_ids": ["evt_does_not_exist"],
                "recommended_action": "none",
                "reason": "Test hallucination",
            }

        resp = lambda_handler_fg(valid_fg_event, invoker=hallucinating_invoker)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["proposal"]["evidence_ids"] == []

        fallback_logs = [
            l for l in logs
            if l.get("event") == "focus_guardian_safe_fallback_activated"
        ]
        assert fallback_logs, "Expected a safe_fallback log record"
        assert fallback_logs[0]["fallback_reason"] == "hallucinated_evidence"

    def test_validation_error_fallback_reason_logged(
        self, monkeypatch: pytest.MonkeyPatch, valid_fg_event
    ):
        """Model returns a malformed proposal → fallback_reason == 'validation_error'."""
        logs = capture_logs(monkeypatch, fg)

        def bad_schema_invoker(_prompt: str) -> dict:
            # emotion_tone value is not in the allowed Literal set
            return {
                "prep_summary": "Bad schema output.",
                "3_questions": ["Q1?", "Q2?", "Q3?"],
                "emotion_tone": "angry",          # invalid value
                "is_distracted": False,
                "distraction_score": 0.0,
                "deep_work_bank_delta": 0,
                "evidence_ids": [],
                "recommended_action": "none",
                "reason": "Test validation error",
            }

        resp = lambda_handler_fg(valid_fg_event, invoker=bad_schema_invoker)

        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["proposal"]["evidence_ids"] == []

        fallback_logs = [
            l for l in logs
            if l.get("event") == "focus_guardian_safe_fallback_activated"
        ]
        assert fallback_logs, "Expected a safe_fallback log record"
        assert fallback_logs[0]["fallback_reason"] == "validation_error"

    def test_fallback_log_record_structure(
        self, monkeypatch: pytest.MonkeyPatch, valid_fg_event
    ):
        """The safe_fallback log record contains the required structured fields."""
        logs = capture_logs(monkeypatch, fg)

        lambda_handler_fg(valid_fg_event)

        fallback_logs = [
            l for l in logs
            if l.get("event") == "focus_guardian_safe_fallback_activated"
        ]
        assert fallback_logs
        record = fallback_logs[0]

        required_keys = {
            "timestamp", "level", "service", "event",
            "user_id", "session_id", "purpose",
            "explicit_signal", "evidence_count", "fallback_reason",
        }
        missing = required_keys - record.keys()
        assert not missing, f"Log record missing keys: {missing}"
        assert record["service"] == "aura.focus_guardian"
        assert record["level"] == "WARNING"

    def test_invalid_request_returns_400_no_fallback_log(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A malformed input envelope (bad user_id) yields 400; no fallback log emitted."""
        logs = capture_logs(monkeypatch, fg)

        resp = lambda_handler_fg({"user_id": "x"})  # too short + missing fields

        assert resp["statusCode"] == 400
        body = json.loads(resp["body"])
        assert body["error"] == "invalid_request"

        fallback_logs = [
            l for l in logs
            if l.get("event") == "focus_guardian_safe_fallback_activated"
        ]
        assert not fallback_logs, "No fallback should be logged for a 400 rejection"


# ===========================================================================
# WebSocket Handler — correlation_id and query-string token warning contract
# ===========================================================================

class TestWebsocketHandlerCorrelationContract:
    """Verify correlation_id is attached to relay responses and QS token warnings fire."""

    def test_relay_response_contains_correlation_id_in_log(
        self, monkeypatch: pytest.MonkeyPatch, valid_relay_body
    ):
        """relay_accepted log record must carry a non-empty correlation_id."""
        logs = capture_logs(monkeypatch, ws)

        mock_bus = MagicMock()
        resp = handle_relay(valid_relay_body, bus_client=mock_bus, authenticated_user_id="usr_contract_01")

        assert resp["statusCode"] == 200

        accepted_logs = [
            l for l in logs
            if l.get("event") == "relay_accepted"
        ]
        assert accepted_logs, "Expected a relay_accepted log record"
        record = accepted_logs[0]
        assert "correlation_id" in record
        cid = record["correlation_id"]
        assert isinstance(cid, str) and len(cid) > 0, "correlation_id must be a non-empty string"

    def test_relay_accepted_log_structure(
        self, monkeypatch: pytest.MonkeyPatch, valid_relay_body
    ):
        """relay_accepted log record contains the required structured fields."""
        logs = capture_logs(monkeypatch, ws)

        mock_bus = MagicMock()
        handle_relay(valid_relay_body, bus_client=mock_bus, authenticated_user_id="usr_contract_01")

        accepted_logs = [l for l in logs if l.get("event") == "relay_accepted"]
        assert accepted_logs
        record = accepted_logs[0]

        required_keys = {
            "timestamp", "level", "service", "event",
            "correlation_id", "event_id", "event_type",
            "ack_id", "bus_message_id", "evidence_count",
        }
        missing = required_keys - record.keys()
        assert not missing, f"relay_accepted log missing keys: {missing}"
        assert record["service"] == "aura.websocket_handler"

    def test_querystring_token_emits_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Token supplied in query-string (not Authorization header) must emit a WARNING."""
        logs = capture_logs(monkeypatch, ws)

        mock_table = MagicMock()
        token = auth_mod.create_test_jwt("usr_contract_01")
        event = {
            "requestContext": {"connectionId": "conn_qs_test"},
            "queryStringParameters": {
                "auth_token": token,
                "user_id": "usr_contract_01",
            },
            # Deliberately NO Authorization header
        }
        resp = handle_connect(event, table=mock_table)

        assert resp["statusCode"] == 200

        qs_warn_logs = [
            l for l in logs
            if l.get("event") == "connect_token_from_querystring"
        ]
        assert qs_warn_logs, "Expected a WARNING about query-string token"
        record = qs_warn_logs[0]
        assert record["level"] == "WARNING"
        assert record["connection_id"] == "conn_qs_test"
        assert "note" in record
        assert "querystring" in record["note"].lower() or "query" in record["note"].lower()

    def test_header_token_does_not_emit_querystring_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Token supplied via Authorization header must NOT trigger the QS warning."""
        logs = capture_logs(monkeypatch, ws)

        mock_table = MagicMock()
        token = auth_mod.create_test_jwt("usr_header_test")
        event = {
            "requestContext": {"connectionId": "conn_header_test"},
            "headers": {"Authorization": f"Bearer {token}"},
            "queryStringParameters": {},
        }
        resp = handle_connect(event, table=mock_table)

        assert resp["statusCode"] == 200

        qs_warn_logs = [
            l for l in logs
            if l.get("event") == "connect_token_from_querystring"
        ]
        assert not qs_warn_logs, "No QS-token warning expected when header token is used"

    def test_relay_invalid_envelope_log_contains_correlation_id(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Even on a 400 relay error, the log record must carry a correlation_id."""
        logs = capture_logs(monkeypatch, ws)

        mock_bus = MagicMock()
        # Missing event_type and user_id
        bad_body = {"action": "relay", "event_id": "evt_bad_contract"}
        resp = handle_relay(bad_body, bus_client=mock_bus, authenticated_user_id="usr_contract_01")

        assert resp["statusCode"] == 400

        error_logs = [
            l for l in logs
            if l.get("event") == "relay_envelope_invalid"
        ]
        assert error_logs, "Expected relay_envelope_invalid log record"
        assert "correlation_id" in error_logs[0]
        cid = error_logs[0]["correlation_id"]
        assert isinstance(cid, str) and len(cid) > 0
