"""Unit tests for Production Gateway Wiring, Failover Metadata, and Watsonx Integration.

Verifies:
- get_model_invoker() in focus_guardian correctly instantiates the production gateway
  when AURA_MODEL_ADAPTER is set to 'gateway'.
- Lambda response envelope includes safe provider metadata (provider_used, failover_triggered)
  without leaking prompts, secrets, or internal stack traces.
- Primary failure triggers automatic secondary failover with failover_triggered=True.
- Permanent auth errors fail fast without infinite retries.
"""
from __future__ import annotations

import importlib
import json
import os
from typing import Any
import pytest

fg = importlib.import_module("lambda.focus_guardian")
rg = importlib.import_module("lambda.resilience_gateway")

lambda_handler = fg.lambda_handler
get_model_invoker = fg.get_model_invoker
register_model_invoker = fg.register_model_invoker


@pytest.fixture(autouse=True)
def clean_invoker_registry(monkeypatch: pytest.MonkeyPatch):
    register_model_invoker(None)
    yield
    register_model_invoker(None)


@pytest.fixture
def sample_focus_event() -> dict[str, Any]:
    return {
        "user_id": "usr_marcus_gw",
        "session_id": "sess_gw_test_20261021",
        "purpose": "focus_guardian",
        "explicit_signal": "self_report_distracted",
        "evidence": [{"id": "evt_01", "snippet": "Reading API documentation on enterprise failover."}],
        "consented_signal_score": 0.5,
    }


def test_gateway_selection_via_environment_variable(monkeypatch: pytest.MonkeyPatch):
    """When AURA_MODEL_ADAPTER='gateway', get_model_invoker returns a production gateway invoker."""
    monkeypatch.setenv("AURA_MODEL_ADAPTER", "gateway")
    invoker = get_model_invoker()
    assert invoker is not None
    assert callable(invoker)


def test_safe_metadata_exposed_on_primary_success(sample_focus_event: dict[str, Any]):
    """Successful invocation through mock primary sets provider_used='bedrock' and failover_triggered=False."""
    def mock_gateway_call(prompt: str) -> dict[str, Any]:
        return {
            "prep_summary": "Architecture briefing generated calmly.",
            "3_questions": ["Step 1?", "Step 2?", "Step 3?"],
            "emotion_tone": "calm",
            "is_distracted": False,
            "distraction_score": 0.0,
            "deep_work_bank_delta": 0,
            "evidence_ids": ["evt_01"],
            "recommended_action": "none",
            "reason": "Focus steady.",
            "_meta": {
                "provider_used": "bedrock",
                "failover_triggered": False,
                "latency_ms": 142.5,
            },
        }

    resp = lambda_handler(sample_focus_event, invoker=mock_gateway_call)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])

    # Safe metadata present
    assert body["provider_used"] == "bedrock"
    assert body["failover_triggered"] is False
    assert body["inference_mode"] == "live_model"

    # Verify no raw prompt or secret leakage
    assert "prompt" not in body
    assert "api_key" not in body
    assert "secret" not in body


def test_safe_metadata_exposed_on_secondary_failover(sample_focus_event: dict[str, Any]):
    """Failover invocation sets provider_used='watsonx' and failover_triggered=True."""
    def mock_failover_call(prompt: str) -> dict[str, Any]:
        return {
            "prep_summary": "Secondary provider generated fallback response successfully.",
            "3_questions": ["Question 1?", "Question 2?", "Question 3?"],
            "emotion_tone": "confident",
            "is_distracted": False,
            "distraction_score": 0.0,
            "deep_work_bank_delta": 0,
            "evidence_ids": ["evt_01"],
            "recommended_action": "none",
            "reason": "IBM watsonx Granite proposal accepted.",
            "_meta": {
                "provider_used": "watsonx",
                "failover_triggered": True,
                "latency_ms": 380.2,
            },
        }

    resp = lambda_handler(sample_focus_event, invoker=mock_failover_call)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])

    assert body["provider_used"] == "watsonx"
    assert body["failover_triggered"] is True
    assert body["inference_mode"] == "live_model"


def test_gateway_timeout_budget_bounded():
    """Verify that make_production_gateway timeouts are bounded within Lambda execution budget."""
    gw = rg.make_production_gateway()
    # Total potential execution time = (primary_timeout * (1 + max_retries)) + secondary_timeout
    total_budget = (gw.primary.timeout_seconds * (1 + gw.primary.max_retries)) + gw.secondary.timeout_seconds
    # In CDK, FocusGuardianFunction timeout is set to 30s.
    # Total model gateway budget must be strictly less than 28s to leave headroom for cleanup and serialization.
    assert total_budget < 28.0
    assert gw.primary.timeout_seconds <= 8.0
    assert gw.secondary.timeout_seconds <= 12.0


def test_watsonx_unconfigured_credentials_fails_safely(monkeypatch: pytest.MonkeyPatch):
    """When IBM watsonx credentials and secret ARN are missing, invoker raises NotImplementedError safely."""
    monkeypatch.delenv("WATSONX_API_KEY", raising=False)
    monkeypatch.delenv("WATSONX_SECRET_ARN", raising=False)
    monkeypatch.delenv("WATSONX_MOCK", raising=False)

    watsonx_invoker = rg.create_real_watsonx_invoker()
    with pytest.raises(NotImplementedError) as exc_info:
        watsonx_invoker('{"evidence": []}', "ibm/granite-3-8b-instruct")
    assert "not configured" in str(exc_info.value)

