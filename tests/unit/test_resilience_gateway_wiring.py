"""Unit tests for Phase 3: Connecting Resilience Gateway to Real Adapters."""
from __future__ import annotations

import importlib
import time
from typing import Any
import pytest

rg = importlib.import_module("lambda.resilience_gateway")
MultiProviderModelGateway = rg.MultiProviderModelGateway
ProviderConfig = rg.ProviderConfig
ProviderThrottleError = rg.ProviderThrottleError
ProviderTimeoutError = rg.ProviderTimeoutError
ProviderServerError = rg.ProviderServerError
ProviderError = rg.ProviderError
AllProvidersUnavailableError = rg.AllProvidersUnavailableError
make_gateway = rg.make_gateway
make_production_gateway = rg.make_production_gateway
create_real_bedrock_invoker = rg.create_real_bedrock_invoker
create_real_watsonx_invoker = rg.create_real_watsonx_invoker


_SAMPLE_PROMPT = '{"purpose": "pre_meeting", "evidence": [{"id": "evt_01", "snippet": "Architecture review"}]}'

_VALID_PROPOSAL_PAYLOAD = {
    "prep_summary": "Architecture session briefing prepared.",
    "3_questions": ["What is component A?", "What is component B?", "What is metric C?"],
    "emotion_tone": "calm",
    "is_distracted": False,
    "distraction_score": 0.0,
    "deep_work_bank_delta": 0,
    "evidence_ids": ["evt_01"],
    "recommended_action": "none",
    "reason": "Briefing generated successfully.",
}


def test_primary_success():
    """Test 1: Primary provider succeeds immediately."""
    primary_called = []

    def mock_primary(prompt: str, model_id: str) -> dict[str, Any]:
        primary_called.append((prompt, model_id))
        return dict(_VALID_PROPOSAL_PAYLOAD)

    gw = make_gateway(bedrock_invoke=mock_primary)
    result = gw.invoke(_SAMPLE_PROMPT)

    assert result.provider == "bedrock"
    assert result.failover_triggered is False
    assert len(primary_called) == 1
    assert result.payload["prep_summary"] == _VALID_PROPOSAL_PAYLOAD["prep_summary"]


def test_retry_on_transient_error():
    """Test 2: Transient error (429) retries up to max_retries and succeeds."""
    attempts = [0]

    def retry_primary(prompt: str, model_id: str) -> dict[str, Any]:
        attempts[0] += 1
        if attempts[0] == 1:
            raise ProviderThrottleError("Rate limit exceeded", status_code=429)
        return dict(_VALID_PROPOSAL_PAYLOAD)

    gw = make_gateway(bedrock_invoke=retry_primary, primary_retries=2)
    result = gw.invoke(_SAMPLE_PROMPT)

    assert result.provider == "bedrock"
    assert result.failover_triggered is False
    assert attempts[0] == 2


def test_primary_failure_with_secondary_success():
    """Test 3: Primary exhausts retries and fails over to secondary successfully."""
    primary_attempts = [0]
    secondary_called = [0]

    def failing_primary(prompt: str, model_id: str) -> dict[str, Any]:
        primary_attempts[0] += 1
        raise ProviderServerError("Bedrock 503 Service Unavailable", status_code=503)

    def ok_secondary(prompt: str, model_id: str) -> dict[str, Any]:
        secondary_called[0] += 1
        return dict(_VALID_PROPOSAL_PAYLOAD)

    gw = make_gateway(
        bedrock_invoke=failing_primary,
        watsonx_invoke=ok_secondary,
        primary_retries=1,
    )
    result = gw.invoke(_SAMPLE_PROMPT)

    assert result.provider == "watsonx"
    assert result.failover_triggered is True
    assert primary_attempts[0] == 2  # 1 initial + 1 retry
    assert secondary_called[0] == 1


def test_both_providers_fail_raises_all_unavailable():
    """Test 4: When both providers fail, raises AllProvidersUnavailableError."""
    def fail_p(prompt: str, model_id: str) -> dict[str, Any]:
        raise ProviderThrottleError("Bedrock throttled", status_code=429)

    def fail_s(prompt: str, model_id: str) -> dict[str, Any]:
        raise ProviderServerError("watsonx 500 internal error", status_code=500)

    gw = make_gateway(bedrock_invoke=fail_p, watsonx_invoke=fail_s, primary_retries=0)
    with pytest.raises(AllProvidersUnavailableError) as exc_info:
        gw.invoke(_SAMPLE_PROMPT)

    err_text = str(exc_info.value)
    assert "Both providers failed" in err_text
    assert "Bedrock throttled" in err_text
    assert "watsonx 500" in err_text


def test_permanent_auth_error_does_not_retry():
    """Test 5: Permanent auth errors (401/403) fail immediately without useless retries."""
    attempts = [0]

    def auth_error_primary(prompt: str, model_id: str) -> dict[str, Any]:
        attempts[0] += 1
        raise ProviderError("AccessDenied: Invalid IAM role", status_code=403)

    def ok_secondary(prompt: str, model_id: str) -> dict[str, Any]:
        return dict(_VALID_PROPOSAL_PAYLOAD)

    gw = make_gateway(
        bedrock_invoke=auth_error_primary,
        watsonx_invoke=ok_secondary,
        primary_retries=3,  # Should NOT be retried
    )
    result = gw.invoke(_SAMPLE_PROMPT)

    assert attempts[0] == 1  # Exactly 1 attempt, zero retries
    assert result.provider == "watsonx"
    assert result.failover_triggered is True


def test_missing_credentials_fails_safely():
    """Test 6: Missing credentials raise NotImplementedError which safely triggers failover."""
    def unconfigured_primary(prompt: str, model_id: str) -> dict[str, Any]:
        raise NotImplementedError("Bedrock credentials missing")

    def ok_secondary(prompt: str, model_id: str) -> dict[str, Any]:
        return dict(_VALID_PROPOSAL_PAYLOAD)

    gw = make_gateway(
        bedrock_invoke=unconfigured_primary,
        watsonx_invoke=ok_secondary,
        primary_retries=2,
    )
    result = gw.invoke(_SAMPLE_PROMPT)
    assert result.provider == "watsonx"
    assert result.failover_triggered is True


def test_enforced_wall_clock_timeout():
    """Test 7: Timeout is genuinely enforced via thread pool deadline."""
    def hanging_primary(prompt: str, model_id: str) -> dict[str, Any]:
        time.sleep(0.5)  # Exceeds the 0.05s timeout
        return dict(_VALID_PROPOSAL_PAYLOAD)

    def ok_secondary(prompt: str, model_id: str) -> dict[str, Any]:
        return dict(_VALID_PROPOSAL_PAYLOAD)

    gw = make_gateway(
        bedrock_invoke=hanging_primary,
        watsonx_invoke=ok_secondary,
        primary_timeout=0.05,  # 50ms deadline
        primary_retries=0,
    )

    t_start = time.monotonic()
    result = gw.invoke(_SAMPLE_PROMPT)
    duration = time.monotonic() - t_start

    # Must have failed over to secondary within reasonable time (< 0.4s)
    assert result.provider == "watsonx"
    assert result.failover_triggered is True
    assert duration < 0.4


def test_make_production_gateway_wires_adapters():
    """Test 8: make_production_gateway constructs configured real provider instances."""
    gw = make_production_gateway()
    assert isinstance(gw, MultiProviderModelGateway)
    assert gw.primary.name == "bedrock"
    assert gw.secondary.name == "watsonx"
    assert gw.primary.timeout_seconds == 5.0
    assert gw.secondary.timeout_seconds == 8.0


def test_bedrock_error_propagation_triggers_watsonx(monkeypatch):
    """Test 9: Real Bedrock adapter errors propagate to the gateway and trigger Watsonx."""
    ba = importlib.import_module("lambda.bedrock_adapter")
    wa = importlib.import_module("lambda.watsonx_adapter")

    # Set mock mode for watsonx so secondary produces valid proposal
    monkeypatch.setenv("WATSONX_MOCK", "true")
    # Simulate Bedrock runtime failure
    monkeypatch.setenv("BEDROCK_MOCK", "true")
    monkeypatch.setenv("BEDROCK_SIMULATE_ERROR", "ThrottlingException: Bedrock rate limit exceeded")

    gw = make_gateway(wire_real_adapters=True, primary_retries=0)
    res = gw.invoke(_SAMPLE_PROMPT)

    assert res.provider == "watsonx"
    assert res.failover_triggered is True
    assert "prep_summary" in res.payload


def test_both_providers_fail_produces_focus_guardian_safe_fallback(monkeypatch):
    """Test 10: When both Bedrock and Watsonx fail, focus_guardian produces safe fallback."""
    fg = importlib.import_module("lambda.focus_guardian")

    # Both providers simulate failure
    monkeypatch.setenv("BEDROCK_MOCK", "true")
    monkeypatch.setenv("BEDROCK_SIMULATE_ERROR", "Bedrock 500 Service Error")
    monkeypatch.setenv("WATSONX_MOCK", "true")
    monkeypatch.setenv("WATSONX_SIMULATE_ERROR", "watsonx 500 Service Error")

    gw = make_gateway(wire_real_adapters=True, primary_retries=0)

    def _gw_call(prompt: str) -> dict[str, Any]:
        return gw.invoke(prompt).payload

    event = {
        "user_id": "usr_failover_test",
        "session_id": "sess_failover_001",
        "purpose": "focus_guardian",
        "explicit_signal": "none",
        "evidence": [{"id": "evt_01", "snippet": "Normal work session"}],
    }

    res = fg.lambda_handler(event, invoker=_gw_call)
    assert res["statusCode"] == 200
    import json
    body = json.loads(res["body"])
    assert body["fallback_active"] is True
    assert body["fallback_reason"] == "provider_error"
    assert body["inference_mode"] == "fallback"


def test_bedrock_success_avoids_watsonx_failover(monkeypatch):
    """Test 11: When Bedrock succeeds, Watsonx is never invoked."""
    monkeypatch.setenv("BEDROCK_MOCK", "true")
    monkeypatch.delenv("BEDROCK_SIMULATE_ERROR", raising=False)

    watsonx_called = [0]
    def tracking_watsonx(prompt: str, model_id: str) -> dict[str, Any]:
        watsonx_called[0] += 1
        return dict(_VALID_PROPOSAL_PAYLOAD)

    gw = make_gateway(
        bedrock_invoke=create_real_bedrock_invoker(),
        watsonx_invoke=tracking_watsonx,
        primary_retries=0,
    )
    res = gw.invoke(_SAMPLE_PROMPT)

    assert res.provider == "bedrock"
    assert res.failover_triggered is False
    assert watsonx_called[0] == 0
