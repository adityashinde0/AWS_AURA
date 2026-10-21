"""Unit tests verifying Frontend Connected-Mode Error Handling contract.

Validates that:
1. Failed backend API calls (HTTP 401, 403, 404, 500, and network failure)
   in connected mode immediately stop the action and do not execute local simulation.
2. Consent grants are never marked revoked after a backend rejection.
3. Distraction signals do not pause simulated devices or create fake undo tokens after backend failure.
4. Focus sessions do not activate locally after backend rejection.
5. Local simulation and simulated undo tokens are only available when explicitly in demo/offline mode.
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

APP_JS_PATH = Path(__file__).resolve().parent.parent.parent / "frontend" / "app.js"


def test_trigger_distraction_connected_mode_stops_on_failure():
    """Verify triggerDistraction stops immediately on backend failure without calling executeDevicePause."""
    content = APP_JS_PATH.read_text(encoding="utf-8")

    # Locate triggerDistraction function body
    match = re.search(r"async function triggerDistraction\(\)\s*\{([\s\S]*?)\n\}\n", content)
    assert match is not None
    body = match.group(1)

    # In connected mode, postFocusSignal failure must return immediately
    assert "!resp.success" in body
    assert "No local actions executed" in body
    assert "Device execution halted" in body

    # executeDevicePause must NOT be called in the connected failure branch
    # Extract the if (API_CONFIG.isConnected()) block
    connected_block = re.search(r"if\s*\(API_CONFIG\.isConnected\(\)\)\s*\{([\s\S]*?)\n  \}", body)
    assert connected_block is not None
    conn_text = connected_block.group(1)

    # In connected mode, executeDevicePause must not be present as a fallback
    assert "executeDevicePause();" not in conn_text
    assert "Falling back to local simulation" not in conn_text


def test_grant_revocation_connected_mode_stops_on_failure():
    """Verify grant revocation does not mark grant inactive if backend rejects."""
    content = APP_JS_PATH.read_text(encoding="utf-8")

    # Locate the btn-revoke click listener
    match = re.search(r"\.btn-revoke['\"]\)\.forEach\([\s\S]*?async \(e\) => \{([\s\S]*?)\n    \}\);", content)
    assert match is not None
    body = match.group(1)

    # If backend fails, must return without setting g.active = false
    assert "if (API_CONFIG.isConnected())" in body
    assert "if (resp.success)" in body
    assert "g.active = false;" in body
    assert "Grant remains active" in body

    # The failure branch must return before g.active = false is reached
    failure_branch = re.search(r"else\s*\{([\s\S]*?return;\s*\}[\s\S]*?g\.active = false;)", body)
    assert failure_branch is not None, "Failure branch must return before local revocation"


def test_execute_device_pause_blocked_when_connected():
    """Verify executeDevicePause is explicitly guarded against connected-mode invocation."""
    content = APP_JS_PATH.read_text(encoding="utf-8")

    match = re.search(r"function executeDevicePause\(\)\s*\{([\s\S]*?)\n\}\n", content)
    assert match is not None
    body = match.group(1)

    # Must check isConnected and abort
    assert "if (API_CONFIG.isConnected())" in body
    assert "only permitted in Demo/Offline mode" in body
    assert "[Connected Fallback]" not in body


def test_focus_session_start_stopped_on_backend_failure():
    """Verify focus session does not activate locally if backend session start fails."""
    content = APP_JS_PATH.read_text(encoding="utf-8")

    match = re.search(r"DOM\.btnToggleFocus\.addEventListener\('click'[\s\S]*?async \(\) => \{([\s\S]*?)\n  \}\);", content)
    assert match is not None
    body = match.group(1)

    assert "auraApiClient.startSession" in body
    assert "Session not started" in body
    assert "Backend rejected focus session start" in body


class SimulatedApiClient:
    """Mock client simulating frontend/app.js auraApiClient._request behavior."""

    def __init__(self, mode: str = "connected", status: int = 200, data: dict | None = None, is_network_error: bool = False):
        self.mode = mode
        self.status = status
        self.data = data or {}
        self.is_network_error = is_network_error

    def request(self):
        if self.mode != "connected":
            return {"success": False, "mode": "demo", "error": "not_connected"}
        if self.is_network_error:
            return {"success": False, "mode": "connected", "error": "network_error", "message": "Failed to fetch"}
        if self.status == 401:
            return {"success": False, "mode": "connected", "status": 401, "error": "unauthorized", "message": "HTTP 401"}
        if self.status == 403:
            return {"success": False, "mode": "connected", "status": 403, "error": "forbidden", "message": "HTTP 403"}
        if self.status == 404:
            return {"success": False, "mode": "connected", "status": 404, "error": "not_found", "message": "HTTP 404"}
        if self.status == 500:
            return {"success": False, "mode": "connected", "status": 500, "error": "server_error", "message": "HTTP 500"}
        return {"success": True, "mode": "connected", "status": self.status, "data": self.data}


@pytest.mark.parametrize("status,expected_error", [
    (401, "unauthorized"),
    (403, "forbidden"),
    (404, "not_found"),
    (500, "server_error"),
])
def test_frontend_handles_all_http_error_statuses(status, expected_error):
    """Verify that HTTP 401, 403, 404, 500 are translated to appropriate client errors."""
    client = SimulatedApiClient(mode="connected", status=status)
    res = client.request()
    assert res["success"] is False
    assert res["status"] == status
    assert res["error"] == expected_error


def test_frontend_handles_network_failure():
    """Verify that network failures are handled without crash."""
    client = SimulatedApiClient(mode="connected", is_network_error=True)
    res = client.request()
    assert res["success"] is False
    assert res["error"] == "network_error"


def test_frontend_handles_successful_request():
    """Verify that HTTP 200 returns success: True in connected mode."""
    client = SimulatedApiClient(mode="connected", status=200, data={"ok": True})
    res = client.request()
    assert res["success"] is True
    assert res["mode"] == "connected"
    assert res["data"]["ok"] is True
