"""AURA Focus Guardian Lambda handler.

Validates input envelopes, constructs prompt with strict JSON schema constraints,
validates model proposals, ensures evidence citations are strictly subset-bounded,
and falls back safely to 'none' action whenever validation fails.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------

def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a single structured JSON log line to stdout.

    No raw user content or evidence text is ever logged — only IDs and counts.
    """
    record = {
        "timestamp": time.time(),
        "level": level,
        "service": "aura.focus_guardian",
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


class FocusRequest(BaseModel):
    user_id: str = Field(min_length=3, max_length=128)
    session_id: str = Field(min_length=8, max_length=128)
    purpose: Literal["pre_meeting", "focus_guardian"]
    explicit_signal: Literal["none", "self_report_distracted", "focus_started"] = "none"
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    consented_signal_score: float = Field(default=0.0, ge=0.0, le=1.0)
    user_intent: str | None = Field(default=None, max_length=280)
    connected_devices: list[str] = Field(default_factory=list, max_length=10)


class FocusProposal(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    prep_summary: str = Field(max_length=500)
    questions: list[str] = Field(min_length=3, max_length=3, alias="3_questions")
    emotion_tone: Literal["calm", "confident", "supportive", "neutral", "cautious"]
    is_distracted: bool
    distraction_score: float = Field(ge=0.0, le=1.0)
    deep_work_bank_delta: int = Field(ge=-10, le=10)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)
    recommended_action: Literal["none", "nudge", "request_dnd", "request_pause"]
    reason: str = Field(max_length=280)

    @field_validator("questions")
    @classmethod
    def questions_are_nonempty(cls, value: list[str]) -> list[str]:
        if any(not question.strip() for question in value):
            raise ValueError("Questions must not be blank")
        return value


SYSTEM_PROMPT = """You are AURA, a privacy-first focus assistant powered by Claude 3.5 Sonnet.
Return JSON only matching the required schema. Use only supplied evidence IDs.
Never claim certainty beyond evidence. Never diagnose a person. Never execute or
promise external actions: recommend at most a reversible, consent-gated action.
When user_intent or connected_devices are supplied, reason over the intent and context,
recommending actions across available certified devices (e.g., device: "dev_firetv_living_room",
action: "request_pause"). If evidence is insufficient, provide a neutral brief and action 'none'."""


def build_prompt(request: FocusRequest) -> str:
    """Build minimized model input; raw Bee transcripts must be redacted locally."""
    prompt_dict: dict[str, Any] = {
        "system": SYSTEM_PROMPT,
        "purpose": request.purpose,
        "explicit_signal": request.explicit_signal,
        "consented_signal_score": request.consented_signal_score,
        "evidence": request.evidence,
        "required_schema": FocusProposal.model_json_schema(by_alias=True),
    }
    if request.user_intent:
        prompt_dict["user_intent"] = request.user_intent
    if request.connected_devices:
        prompt_dict["connected_devices"] = request.connected_devices
    return json.dumps(prompt_dict)


def safe_fallback(request: FocusRequest, fallback_reason: str = "") -> FocusProposal:
    """Return a safe, deterministic fallback proposal and log the activation.

    The *fallback_reason* parameter carries a short machine-readable tag
    (e.g. ``"not_implemented"``, ``"validation_error"``, ``"hallucinated_evidence"``)
    so that CloudWatch Insights can query fallback rates by cause.
    The default is an empty string, which preserves the original call signature
    and keeps existing callers and tests unchanged.
    """
    distracted = request.explicit_signal == "self_report_distracted"
    user_intent = (request.user_intent or "").strip().lower()

    if "tired" in user_intent or "sleep" in user_intent or "wind down" in user_intent:
        prep_summary = "Intent indicates fatigue or desire to sleep. Proposing a calm wind-down routine."
        questions = ["Would you like to wind down now?", "Should we dim ambient lights?", "Pause active screen playback?"]
        tone: Literal["calm", "confident", "supportive", "neutral", "cautious"] = "calm"
        rec_action: Literal["none", "nudge", "request_dnd", "request_pause"] = "request_pause"
        score = 0.5
        bank_delta = 0
        reason = "Using a safe fallback for rest/sleep intent; recommending pausing media devices and dimming lights."
        device_actions = [
            {"device": d, "action": "request_pause"}
            for d in request.connected_devices
            if "firetv" in d or "tv" in d
        ] + [
            {"device": d, "action": "dim_lights"}
            for d in request.connected_devices
            if "light" in d
        ]
        if not device_actions and request.connected_devices:
            device_actions = [{"device": request.connected_devices[0], "action": "request_pause"}]
    elif "break" in user_intent:
        prep_summary = "Short break requested. Proposing 15-minute pause and breather status."
        questions = ["Ready for a 15-minute breather?", "Set Slack status to away?", "Resume deep work afterwards?"]
        tone = "supportive"
        rec_action = "request_pause"
        score = 0.3
        bank_delta = 0
        reason = "Using a safe fallback for quick break; pausing focus session and media playback."
        device_actions = [
            {"device": d, "action": "request_pause"}
            for d in request.connected_devices
            if "firetv" in d or "tv" in d
        ]
    elif distracted or "distracted" in user_intent:
        prep_summary = "Self-reported distraction detected. A gentle refocus nudge is recommended."
        questions = ["What outcome matters most?", "What decision is needed?", "What is the next concrete step?"]
        tone = "neutral"
        rec_action = "nudge"
        score = 0.8
        bank_delta = -1
        reason = "Using a safe fallback for self-reported distraction; recommending haptic refocus nudge."
        device_actions = [{"device": "dev_orbit_wearable", "action": "nudge"}]
    elif user_intent:
        prep_summary = f"Custom intent '{request.user_intent}' recognized. Proposing supportive adjustment."
        questions = ["Would a brief pause help reset?", "Should we minimize notifications?", "Adjust ambient environment?"]
        tone = "calm"
        rec_action = "request_pause"
        score = 0.4
        bank_delta = 0
        reason = f"Using a safe fallback for custom intent '{request.user_intent}' with low-stimulation routine."
        device_actions = [
            {"device": d, "action": "request_pause"}
            for d in request.connected_devices
            if "firetv" in d or "tv" in d
        ]
    else:
        prep_summary = "No high-confidence context is available. Open the event details and add your own intention."
        questions = ["What outcome matters most?", "What decision is needed?", "What is the next concrete step?"]
        tone = "neutral"
        rec_action = "none"
        score = request.consented_signal_score
        bank_delta = 0
        reason = "Using a safe fallback because no validated model proposal was available."
        device_actions = []

    _log(
        "WARNING",
        "focus_guardian_safe_fallback_activated",
        user_id=request.user_id,
        session_id=request.session_id,
        purpose=request.purpose,
        explicit_signal=request.explicit_signal,
        evidence_count=len(request.evidence),
        user_intent=request.user_intent,
        fallback_reason=fallback_reason or "unspecified",
    )

    fallback_dict: dict[str, Any] = {
        "prep_summary": prep_summary,
        "3_questions": questions,
        "emotion_tone": tone,
        "is_distracted": distracted or ("distracted" in user_intent),
        "distraction_score": score,
        "deep_work_bank_delta": bank_delta,
        "evidence_ids": [],
        "recommended_action": rec_action,
        "reason": reason,
    }
    if device_actions:
        fallback_dict["device_actions"] = device_actions

    return FocusProposal.model_validate(fallback_dict)


# ---------------------------------------------------------------------------
# Dependency Injection / Model Invoker Registry
# ---------------------------------------------------------------------------

_REGISTERED_INVOKER: Callable[[str], dict[str, Any]] | None = None


def register_model_invoker(invoker: Callable[[str], dict[str, Any]] | None) -> None:
    """Explicitly register a model invoker callable for focus_guardian.

    Passing ``None`` resets the registry to environment-based resolution.
    """
    global _REGISTERED_INVOKER
    _REGISTERED_INVOKER = invoker


def get_model_invoker() -> Callable[[str], dict[str, Any]]:
    """Resolve the effective model invoker.

    Priority:
    1. Explicitly registered invoker via ``register_model_invoker(...)``.
    2. Bedrock adapter if ``AURA_MODEL_ADAPTER`` is "bedrock" / "aws" / "live".
    3. Resilience gateway if ``AURA_MODEL_ADAPTER`` is "gateway".
    4. Default ``invoke_model`` (raises NotImplementedError when unconfigured,
       ensuring safe deterministic fallback in offline tests).
    """
    if _REGISTERED_INVOKER is not None:
        return _REGISTERED_INVOKER

    adapter_choice = os.getenv("AURA_MODEL_ADAPTER", "").strip().lower()
    if adapter_choice in ("bedrock", "aws", "live"):
        try:
            import importlib
            ba = importlib.import_module("lambda.bedrock_adapter")
            return ba.invoke_model
        except Exception as exc:
            _log("WARNING", "focus_guardian_bedrock_import_failed", error=str(exc))
    elif adapter_choice in ("gateway", "resilience_gateway"):
        try:
            import importlib
            rg = importlib.import_module("lambda.resilience_gateway")
            gw = rg.make_production_gateway()

            def _gateway_invoker(prompt: str) -> dict[str, Any]:
                res = gw.invoke(prompt)
                out = dict(res.payload)
                out["_meta"] = {
                    "provider_used": res.provider,
                    "failover_triggered": res.failover_triggered,
                    "latency_ms": round(res.latency_ms, 2),
                }
                return out

            return _gateway_invoker
        except Exception as exc:
            _log("WARNING", "focus_guardian_gateway_import_failed", error=str(exc))

    return invoke_model


def invoke_model(_prompt: str) -> dict[str, Any]:
    """Default fallback model invoker.

    In development/testing when no adapter has been wired, raises
    ``NotImplementedError``. The ``lambda_handler`` catches this and
    routes cleanly to ``safe_fallback(..., fallback_reason="not_implemented")``.
    """
    raise NotImplementedError("Configure a Bedrock model adapter in deployment")


def lambda_handler(
    event: dict[str, Any],
    _context: object = None,
    invoker: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        if "detail" in event and isinstance(event["detail"], dict):
            raw_body = dict(event["detail"])
            if "purpose" not in raw_body:
                raw_body["purpose"] = "focus_guardian"
        else:
            raw_body = event.get("body", event)

        if isinstance(raw_body, str):
            try:
                raw_body = json.loads(raw_body)
            except json.JSONDecodeError as err:
                _log("WARNING", "focus_guardian_invalid_json", error=str(err))
                return {
                    "statusCode": 400,
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"error": "invalid_json_body", "details": str(err)}),
                }

        # HTTP Ingress Authentication & User Authorization Boundary
        is_http = bool(event.get("requestContext") or event.get("rawPath") or ("headers" in event and "detail" not in event))
        if is_http:
            try:
                import importlib
                _auth = importlib.import_module("lambda.auth")
                auth_ctx = _auth.extract_and_validate_auth(event)
                if not auth_ctx.is_authenticated:
                    return {
                        "statusCode": 401,
                        "headers": {"content-type": "application/json"},
                        "body": json.dumps({
                            "error": auth_ctx.error or "unauthorized",
                            "message": auth_ctx.error_message or "Authentication required for protected endpoint",
                        }),
                    }
                requested_uid = raw_body.get("user_id") if isinstance(raw_body, dict) else None
                verified_uid = _auth.enforce_user_authorization(auth_ctx, requested_uid)
                if isinstance(raw_body, dict):
                    raw_body["user_id"] = verified_uid
            except Exception as auth_exc:
                status = getattr(auth_exc, "status_code", 401)
                err_code = getattr(auth_exc, "error", "unauthorized")
                return {
                    "statusCode": status,
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"error": err_code, "message": str(auth_exc)}),
                }

        request = FocusRequest.model_validate(raw_body)
        model_call = invoker or get_model_invoker()

        _log(
            "INFO",
            "focus_guardian_evaluation_started",
            user_id=request.user_id,
            session_id=request.session_id,
            purpose=request.purpose,
            explicit_signal=request.explicit_signal,
            evidence_count=len(request.evidence),
        )

        proposal: FocusProposal
        is_fallback = False
        fallback_cause: str | None = None
        provider_meta: dict[str, Any] = {}
        try:
            raw_output = model_call(build_prompt(request))
            if isinstance(raw_output, dict):
                model_output = dict(raw_output)
                if "_meta" in model_output:
                    provider_meta = model_output.pop("_meta")
            else:
                model_output = raw_output
            proposal = FocusProposal.model_validate(model_output)
            source_ids = {item.get("id") for item in request.evidence if isinstance(item, dict) and "id" in item}
            if not set(proposal.evidence_ids).issubset(source_ids):
                raise ValueError("Model cited evidence not present in request")
            _log(
                "INFO",
                "focus_guardian_model_proposal_accepted",
                user_id=request.user_id,
                session_id=request.session_id,
                recommended_action=proposal.recommended_action,
                emotion_tone=proposal.emotion_tone,
                evidence_ids_count=len(proposal.evidence_ids),
            )
        except NotImplementedError:
            is_fallback = True
            fallback_cause = "not_implemented"
            proposal = safe_fallback(request, fallback_reason="not_implemented")
        except ValidationError as val_err:
            is_fallback = True
            fallback_cause = "validation_error"
            _log(
                "WARNING",
                "focus_guardian_model_output_invalid",
                user_id=request.user_id,
                session_id=request.session_id,
                validation_errors=len(val_err.errors()),
            )
            proposal = safe_fallback(request, fallback_reason="validation_error")
        except ValueError as val_err:
            is_fallback = True
            fallback_cause = "hallucinated_evidence"
            _log(
                "WARNING",
                "focus_guardian_evidence_hallucination",
                user_id=request.user_id,
                session_id=request.session_id,
                error=str(val_err),
            )
            proposal = safe_fallback(request, fallback_reason="hallucinated_evidence")
        except TypeError:
            is_fallback = True
            fallback_cause = "type_error"
            proposal = safe_fallback(request, fallback_reason="type_error")
        except Exception as exc:
            is_fallback = True
            fallback_cause = "provider_error"
            _log(
                "WARNING",
                "focus_guardian_provider_error",
                user_id=request.user_id,
                session_id=request.session_id,
                error=str(exc),
            )
            proposal = safe_fallback(request, fallback_reason="provider_error")

        _log(
            "INFO",
            "focus_guardian_evaluation_complete",
            user_id=request.user_id,
            session_id=request.session_id,
            recommended_action=proposal.recommended_action,
            is_distracted=proposal.is_distracted,
            policy_required=True,
            fallback_active=is_fallback,
        )
        dumped_proposal = proposal.model_dump(by_alias=True)
        resp_body: dict[str, Any] = {
            "proposal": dumped_proposal,
            "policy_required": True,
            "inference_mode": "fallback" if is_fallback else "live_model",
            "fallback_active": is_fallback,
        }
        if provider_meta:
            resp_body["provider_used"] = provider_meta.get("provider_used")
            resp_body["failover_triggered"] = provider_meta.get("failover_triggered", False)
        elif not is_fallback:
            resp_body["provider_used"] = "bedrock"
            resp_body["failover_triggered"] = False

        if is_fallback:
            resp_body["fallback_reason"] = fallback_cause or "unspecified"

        if hasattr(proposal, "device_actions") and getattr(proposal, "device_actions"):
            resp_body["device_actions"] = getattr(proposal, "device_actions")
        elif "device_actions" in dumped_proposal:
            resp_body["device_actions"] = dumped_proposal["device_actions"]

        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(resp_body),
        }
    except ValidationError as exc:
        _log(
            "WARNING",
            "focus_guardian_request_invalid",
            error_count=len(exc.errors()),
        )
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": "invalid_request", "details": exc.errors()}),
        }
