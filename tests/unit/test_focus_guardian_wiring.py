"""Unit tests for Phase 2 Focus Guardian Model Wiring and Dependency Injection."""
from __future__ import annotations

import importlib
import json
from typing import Any
import pytest

fg = importlib.import_module("lambda.focus_guardian")
ba = importlib.import_module("lambda.bedrock_adapter")

FocusRequest = fg.FocusRequest
FocusProposal = fg.FocusProposal
lambda_handler = fg.lambda_handler
register_model_invoker = fg.register_model_invoker
get_model_invoker = fg.get_model_invoker


@pytest.fixture(autouse=True)
def reset_invoker_registry():
    """Ensure clean DI registry state before and after each test."""
    register_model_invoker(None)
    yield
    register_model_invoker(None)


@pytest.fixture
def valid_request_envelope() -> dict[str, Any]:
    return {
        "user_id": "usr_aditi_wiring",
        "session_id": "sess_20261021_wiring",
        "purpose": "focus_guardian",
        "explicit_signal": "self_report_distracted",
        "evidence": [
            {"id": "evt_wiring_01", "snippet": "Architecture review preparation for Bedrock Converse API."}
        ],
        "consented_signal_score": 0.4,
    }


def test_1_configured_handler_calls_injected_adapter(valid_request_envelope):
    """Test 1: Handler executes injected adapter and captures invocation count."""
    called_with = []

    def mock_adapter(prompt: str) -> dict[str, Any]:
        called_with.append(prompt)
        return {
            "prep_summary": "Injected adapter executed successfully for focus evaluation.",
            "3_questions": ["What is step 1?", "What is step 2?", "What is step 3?"],
            "emotion_tone": "confident",
            "is_distracted": True,
            "distraction_score": 0.75,
            "deep_work_bank_delta": -1,
            "evidence_ids": ["evt_wiring_01"],
            "recommended_action": "nudge",
            "reason": "Injected adapter validated distraction context.",
        }

    resp = lambda_handler(valid_request_envelope, invoker=mock_adapter)
    assert resp["statusCode"] == 200
    assert len(called_with) == 1
    prompt_payload = json.loads(called_with[0])
    assert prompt_payload["purpose"] == "focus_guardian"
    assert prompt_payload["evidence"][0]["id"] == "evt_wiring_01"

    body = json.loads(resp["body"])
    assert body["inference_mode"] == "live_model"
    assert body["fallback_active"] is False
    assert body["proposal"]["recommended_action"] == "nudge"
    assert body["proposal"]["emotion_tone"] == "confident"


def test_2_valid_model_output_accepted_with_live_status(valid_request_envelope):
    """Test 2: Valid proposal is accepted without fallback activation."""
    valid_output = {
        "prep_summary": "Calm focus session analysis based on provided evidence snippets.",
        "3_questions": ["Question Alpha?", "Question Beta?", "Question Gamma?"],
        "emotion_tone": "calm",
        "is_distracted": False,
        "distraction_score": 0.1,
        "deep_work_bank_delta": 0,
        "evidence_ids": ["evt_wiring_01"],
        "recommended_action": "none",
        "reason": "Evidence suggests focus is steady.",
    }

    resp = lambda_handler(valid_request_envelope, invoker=lambda _: valid_output)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["inference_mode"] == "live_model"
    assert body["fallback_active"] is False
    assert "fallback_reason" not in body
    assert body["proposal"]["recommended_action"] == "none"
    assert body["proposal"]["evidence_ids"] == ["evt_wiring_01"]


def test_3_invalid_model_output_triggers_safe_fallback(valid_request_envelope):
    """Test 3: Schema violation in model output triggers safe fallback honestly."""
    # Missing required '3_questions' and invalid distraction_score (> 1.0)
    malformed_output = {
        "prep_summary": "Missing required fields and out of range score.",
        "emotion_tone": "invalid_tone_not_in_enum",
        "is_distracted": False,
        "distraction_score": 99.9,
    }

    resp = lambda_handler(valid_request_envelope, invoker=lambda _: malformed_output)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["inference_mode"] == "fallback"
    assert body["fallback_active"] is True
    assert body["fallback_reason"] == "validation_error"
    assert "safe fallback" in body["proposal"]["reason"].lower()


def test_4_evidence_hallucination_triggers_safe_fallback(valid_request_envelope):
    """Test 4: Fabricated evidence IDs not present in incoming envelope are rejected."""
    hallucinating_output = {
        "prep_summary": "Fabricated citation test.",
        "3_questions": ["Q1?", "Q2?", "Q3?"],
        "emotion_tone": "neutral",
        "is_distracted": False,
        "distraction_score": 0.0,
        "deep_work_bank_delta": 0,
        "evidence_ids": ["evt_hallucinated_ghost_id"],  # NOT in request evidence!
        "recommended_action": "none",
        "reason": "Cited fabricated ID",
    }

    resp = lambda_handler(valid_request_envelope, invoker=lambda _: hallucinating_output)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["inference_mode"] == "fallback"
    assert body["fallback_active"] is True
    assert body["fallback_reason"] == "hallucinated_evidence"
    assert body["proposal"]["evidence_ids"] == []


def test_5_provider_failures_trigger_fallback(valid_request_envelope):
    """Test 5: Provider runtime exceptions trigger graceful fallback with error tag."""
    def crashing_provider(_prompt: str) -> dict[str, Any]:
        raise ConnectionResetError("Bedrock endpoint socket reset by peer")

    resp = lambda_handler(valid_request_envelope, invoker=crashing_provider)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["inference_mode"] == "fallback"
    assert body["fallback_active"] is True
    assert body["fallback_reason"] == "provider_error"


def test_6_unconfigured_provider_fails_safely(valid_request_envelope):
    """Test 6: Missing credentials/configuration fail closed with not_implemented tag."""
    # When no invoker is provided and default invoke_model is called
    resp = lambda_handler(valid_request_envelope)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["inference_mode"] == "fallback"
    assert body["fallback_active"] is True
    assert body["fallback_reason"] == "not_implemented"


def test_7_register_model_invoker_registry(valid_request_envelope):
    """Test 7: Registered invoker is used when invoker parameter is omitted."""
    custom_called = []

    def registered_invoker(prompt: str) -> dict[str, Any]:
        custom_called.append(True)
        return {
            "prep_summary": "Executed through registered dependency injection registry.",
            "3_questions": ["Q1?", "Q2?", "Q3?"],
            "emotion_tone": "calm",
            "is_distracted": False,
            "distraction_score": 0.0,
            "deep_work_bank_delta": 0,
            "evidence_ids": ["evt_wiring_01"],
            "recommended_action": "none",
            "reason": "Valid registered execution.",
        }

    register_model_invoker(registered_invoker)
    resp = lambda_handler(valid_request_envelope)  # No invoker= passed!
    assert len(custom_called) == 1
    body = json.loads(resp["body"])
    assert body["inference_mode"] == "live_model"
    assert body["fallback_active"] is False
    assert body["proposal"]["prep_summary"] == "Executed through registered dependency injection registry."


def test_8_bedrock_adapter_integration_in_mock_mode(valid_request_envelope):
    """Test 8: Real BedrockAdapter in mock_mode wired into focus_guardian."""
    adapter = ba.BedrockAdapter(mock_mode=True)
    resp = lambda_handler(valid_request_envelope, invoker=adapter.invoke_model)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["inference_mode"] == "live_model"
    assert body["fallback_active"] is False
    assert len(body["proposal"]["3_questions"]) == 3
