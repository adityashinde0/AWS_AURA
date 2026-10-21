"""Amazon Bedrock adapter for Project AURA Focus Guardian.

Calls the Amazon Bedrock Runtime API (Claude or Titan) with structured JSON
output instructions, enforces the FocusProposal contract, validates evidence
citations, resists prompt injection, and provides graceful fail-closed fallback
when the model is unavailable or evidence is insufficient.

This module provides two interfaces:

1. ``BedrockAdapter.generate_briefing(request)`` — high-level class interface
   that mirrors the WatsonxAdapter pattern; used when full request context is
   available.

2. ``invoke_model(prompt: str) -> dict`` — lower-level function that matches
   the ``invoker=`` parameter signature expected by
   ``lambda.focus_guardian.lambda_handler``.  Wire it via::

       from lambda import bedrock_adapter
       result = focus_guardian.lambda_handler(event, invoker=bedrock_adapter.invoke_model)

Environment variables (all optional; defaults shown):
  BEDROCK_MODEL_ID   – Bedrock model ID, default anthropic.claude-3-haiku-20240307-v1:0
  BEDROCK_REGION     – AWS region, default us-east-1
  BEDROCK_MOCK       – set "true" / "1" / "yes" for offline mock mode (no AWS call)
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
from typing import Any

fg = importlib.import_module("lambda.focus_guardian")
FocusProposal: type = fg.FocusProposal
FocusRequest: type = fg.FocusRequest
safe_fallback = fg.safe_fallback
build_prompt = fg.build_prompt

# ---------------------------------------------------------------------------
# Structured logger — same pattern as lambda/focus_guardian.py
# ---------------------------------------------------------------------------

def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a single structured JSON log line to stdout.

    No raw prompt bodies, model responses, or OAuth tokens appear in log fields.
    """
    record = {
        "timestamp": time.time(),
        "level": level,
        "service": "aura.bedrock_adapter",
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0"
)
DEFAULT_REGION = os.getenv("BEDROCK_REGION", "us-east-1")

PROMPT_INJECTION_PATTERNS: list[str] = [
    r"ignore (all )?(previous|above) instructions",
    r"system override",
    r"grant admin consent",
    r"execute fire_?tv_?pause",
    r"execute remote action",
    r"bypass (safety|policy)",
    r"you are now (unrestricted|evil|dan)",
    r"disregard (all )?(previous|prior) (instructions|prompts)",
    r"new instructions:",
    r"from now on you (will|are|must)",
]

SYSTEM_PROMPT = (
    "You are AURA, a privacy-first focus intelligence assistant. "
    "Produce a briefing in strict JSON that matches the provided schema exactly. "
    "Rules:\n"
    "- 'prep_summary': 40–70 words, actionable, calm.\n"
    "- '3_questions': exactly three non-empty, distinct questions.\n"
    "- 'emotion_tone': one of calm | confident | supportive | neutral | cautious.\n"
    "- 'is_distracted': false for pre_meeting purpose.\n"
    "- 'distraction_score': 0.0 for pre_meeting.\n"
    "- 'deep_work_bank_delta': 0.\n"
    "- 'evidence_ids': strict subset of the supplied evidence IDs. "
    "Do NOT invent or hallucinate any ID.\n"
    "- 'recommended_action': one of none | nudge | request_dnd | request_pause.\n"
    "- 'reason': ≤60 words, grounded only in provided evidence.\n"
    "Return JSON only — no markdown fences, no surrounding text."
)


# ---------------------------------------------------------------------------
# BedrockAdapter
# ---------------------------------------------------------------------------

class BedrockAdapter:
    """Manages Amazon Bedrock Runtime invocation for AURA briefings.

    Parameters
    ----------
    model_id:
        Bedrock model ID.  Defaults to ``BEDROCK_MODEL_ID`` env-var.
    region:
        AWS region.  Defaults to ``BEDROCK_REGION`` env-var.
    mock_mode:
        When True, returns a deterministic offline response without calling AWS.
        Defaults to ``BEDROCK_MOCK`` env-var.
    boto3_client:
        Injectable boto3 bedrock-runtime client for testing.  When None the
        real client is created on first use.
    """

    def __init__(
        self,
        model_id: str | None = None,
        region: str | None = None,
        mock_mode: bool | None = None,
        boto3_client: Any | None = None,
        raise_on_error: bool = False,
    ) -> None:
        self.model_id = model_id or DEFAULT_MODEL_ID
        self.region = region or DEFAULT_REGION
        self.mock_mode = (
            mock_mode
            if mock_mode is not None
            else os.getenv("BEDROCK_MOCK", "false").lower() in ("true", "1", "yes")
        )
        self._client = boto3_client  # injected in tests; lazily created in production
        self.raise_on_error = raise_on_error

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_briefing(self, request: FocusRequest) -> FocusProposal:
        """Generate a structured ``FocusProposal`` for *request*.

        Fails closed to ``safe_fallback`` on any error or policy violation.
        """
        # 1. Prompt injection scan across all evidence snippets
        for ev in request.evidence:
            snippet = ev.get("snippet") or ev.get("text") or ""
            if self.has_prompt_injection(snippet):
                _log(
                    "WARNING",
                    "bedrock_adapter_prompt_injection_blocked",
                    user_id=request.user_id,
                    session_id=request.session_id,
                )
                fallback = safe_fallback(request, fallback_reason="validation_error")
                fallback.reason = "Potential prompt injection detected in evidence; fallback enforced."
                return fallback

        # 2. Gather valid evidence IDs present in the incoming request
        valid_evidence_ids = [
            ev["id"]
            for ev in request.evidence
            if isinstance(ev, dict) and ev.get("id")
        ]

        # 3. Dispatch to mock or live path
        if self.mock_mode:
            if os.getenv("BEDROCK_SIMULATE_ERROR"):
                if self.raise_on_error:
                    raise RuntimeError(os.getenv("BEDROCK_SIMULATE_ERROR"))
                return safe_fallback(request, fallback_reason="not_implemented")
            _log(
                "INFO",
                "bedrock_adapter_mock_mode",
                user_id=request.user_id,
                session_id=request.session_id,
                evidence_count=len(valid_evidence_ids),
            )
            return self._mock_generate(request, valid_evidence_ids)

        # 4. Live Bedrock invocation
        try:
            proposal = self._invoke_bedrock(request, valid_evidence_ids)
        except Exception as exc:  # noqa: BLE001 — fail closed on any live-path error
            exc_name = type(exc).__name__
            _log(
                "WARNING",
                "bedrock_adapter_invocation_error",
                user_id=request.user_id,
                session_id=request.session_id,
                error=exc_name,
            )
            if self.raise_on_error:
                raise
            # Distinguish throttling vs other errors for observability
            fallback_reason = (
                "validation_error" if "Throttling" in exc_name else "not_implemented"
            )
            return safe_fallback(request, fallback_reason=fallback_reason)

        # 5. Hallucination guard: all cited IDs must be a subset of request evidence
        if not set(proposal.evidence_ids).issubset(set(valid_evidence_ids)):
            _log(
                "WARNING",
                "bedrock_adapter_hallucinated_evidence",
                user_id=request.user_id,
                session_id=request.session_id,
                hallucinated_count=len(
                    set(proposal.evidence_ids) - set(valid_evidence_ids)
                ),
            )
            return safe_fallback(request, fallback_reason="hallucinated_evidence")

        _log(
            "INFO",
            "bedrock_adapter_proposal_accepted",
            user_id=request.user_id,
            session_id=request.session_id,
            recommended_action=proposal.recommended_action,
            evidence_ids_count=len(proposal.evidence_ids),
        )
        return proposal

    def has_prompt_injection(self, text: str) -> bool:
        """Return True if *text* matches any known prompt-injection pattern."""
        for pattern in PROMPT_INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    # ------------------------------------------------------------------
    # invoke_model — focus_guardian invoker= compatible interface
    # ------------------------------------------------------------------

    def invoke_model(self, prompt: str) -> dict[str, Any]:
        """Accept a serialised prompt string and return a raw dict.

        This is the interface expected by
        ``lambda.focus_guardian.lambda_handler(event, invoker=...)``.
        The prompt produced by ``focus_guardian.build_prompt()`` is a JSON
        string containing ``purpose``, ``evidence``, and ``required_schema``.
        We parse it, reconstruct a minimal ``FocusRequest``, call
        ``generate_briefing()``, and return the proposal as a dict.
        """
        try:
            prompt_data = json.loads(prompt)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("bedrock_adapter.invoke_model received non-JSON prompt")

        # Build a minimal FocusRequest from the prompt context
        evidence = prompt_data.get("evidence", [])
        purpose = prompt_data.get("purpose", "pre_meeting")
        explicit_signal = prompt_data.get("explicit_signal", "none")
        consented_score = float(prompt_data.get("consented_signal_score", 0.0))
        user_intent = prompt_data.get("user_intent")
        connected_devices = prompt_data.get("connected_devices", [])

        request = FocusRequest(
            user_id="adapter_invoke",
            session_id=f"invoke_{int(time.time())}",
            purpose=purpose,
            explicit_signal=explicit_signal,
            evidence=evidence,
            consented_signal_score=consented_score,
            user_intent=user_intent,
            connected_devices=connected_devices,
        )

        proposal = self.generate_briefing(request)
        return proposal.model_dump(by_alias=True)

    # ------------------------------------------------------------------
    # Mock path
    # ------------------------------------------------------------------

    def _mock_generate(
        self, request: FocusRequest, valid_evidence_ids: list[str]
    ) -> FocusProposal:
        """Deterministic offline mock for local development and unit tests."""
        if request.user_intent:
            return safe_fallback(request, fallback_reason="adaptive_intent_mock")

        if not valid_evidence_ids:
            return safe_fallback(request, fallback_reason="validation_error")

        evidence_text = " ".join(
            ev.get("snippet") or ev.get("text") or "" for ev in request.evidence
        )

        tone: str
        if "nervous" in evidence_text.lower():
            tone = "supportive"
        elif "design" in evidence_text.lower() or "architecture" in evidence_text.lower():
            tone = "confident"
        else:
            tone = "calm"

        prep_summary = (
            "You have an upcoming session with relevant context from your recent activity. "
            "Review the key themes, prepare focused questions, and enter the conversation "
            "ready to discuss concrete outcomes. The evidence suggests a clear agenda is available."
        )

        questions = [
            "What is the single most important outcome you need from this session?",
            "Which open decision requires the most input from the other participants?",
            "What concrete next step will you commit to before the session ends?",
        ]

        return FocusProposal(
            prep_summary=prep_summary,
            questions=questions,
            emotion_tone=tone,
            is_distracted=False,
            distraction_score=0.0,
            deep_work_bank_delta=0,
            evidence_ids=valid_evidence_ids[:3],
            recommended_action="none",
            reason="Briefing generated from approved evidence in mock mode.",
        )

    # ------------------------------------------------------------------
    # Live Amazon Bedrock path
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Return a boto3 bedrock-runtime client (lazy init)."""
        if self._client is None:
            try:
                import boto3  # type: ignore[import]
                self._client = boto3.client(
                    "bedrock-runtime", region_name=self.region
                )
            except ImportError as exc:
                raise RuntimeError(
                    "boto3 is required for live Bedrock invocation"
                ) from exc
        return self._client

    def _build_redacted_prompt(
        self, request: FocusRequest, valid_evidence_ids: list[str]
    ) -> str:
        """Build a prompt with redacted evidence for Bedrock.

        Redaction rules:
        - Only structured context keys are sent, never raw names, PII, or
          OAuth tokens.
        - Evidence snippets are included only by their IDs and pre-approved
          snippet text (already redacted upstream by the caller).
        - No ``user_id`` or session data is included in the model payload.
        """
        schema_def = FocusProposal.model_json_schema(by_alias=True)

        # Redact evidence: keep only id and snippet; strip any key that looks
        # like a PII field (email, phone, token, name, etc.)
        _PII_KEYS = frozenset({
            "email", "phone", "name", "full_name", "first_name", "last_name",
            "token", "access_token", "refresh_token", "api_key", "secret",
        })
        redacted_evidence = []
        for ev in request.evidence:
            if not isinstance(ev, dict) or ev.get("id") not in valid_evidence_ids:
                continue
            safe_ev = {
                k: v for k, v in ev.items()
                if k not in _PII_KEYS and isinstance(v, (str, int, float, bool))
            }
            redacted_evidence.append(safe_ev)

        user_content = json.dumps(
            {
                "purpose": request.purpose,
                "explicit_signal": request.explicit_signal,
                "evidence": redacted_evidence,
                "schema": schema_def,
            }
        )

        return f"{SYSTEM_PROMPT}\n\nInput Data:\n{user_content}"

    def _invoke_bedrock(
        self, request: FocusRequest, valid_evidence_ids: list[str]
    ) -> FocusProposal:
        """Call the Bedrock Runtime ``invoke_model`` API and parse the response.

        Supports Claude (messages API via ``anthropic.claude-*`` model IDs)
        and Titan Text (``amazon.titan-text-*``).
        Raises on any ``ClientError`` so the caller can fall back cleanly.
        """
        prompt_text = self._build_redacted_prompt(request, valid_evidence_ids)

        # Determine request body format from model family
        if self.model_id.startswith("anthropic.claude"):
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt_text}],
            })
        else:
            # Amazon Titan Text format
            body = json.dumps({
                "inputText": prompt_text,
                "textGenerationConfig": {
                    "maxTokenCount": 1024,
                    "temperature": 0.2,
                    "stopSequences": [],
                },
            })

        client = self._get_client()
        response = client.invoke_model(
            modelId=self.model_id,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        response_body = json.loads(response["body"].read())

        # Extract generated text from model-specific response shape
        raw_text: str
        if self.model_id.startswith("anthropic.claude"):
            raw_text = response_body["content"][0]["text"]
        else:
            raw_text = response_body["results"][0]["outputText"]

        # Strip optional markdown fences
        cleaned = raw_text.strip()
        for fence in ("```json", "```"):
            if cleaned.startswith(fence):
                cleaned = cleaned[len(fence):]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        proposal: FocusProposal = FocusProposal.model_validate(parsed)

        # Belt-and-suspenders evidence guard
        if not set(proposal.evidence_ids).issubset(set(valid_evidence_ids)):
            raise ValueError("Bedrock model cited evidence IDs not present in request")

        return proposal


# ---------------------------------------------------------------------------
# Module-level invoke_model — wired into focus_guardian as invoker=
# ---------------------------------------------------------------------------

_default_adapter: BedrockAdapter | None = None


def _get_default_adapter() -> BedrockAdapter:
    global _default_adapter
    if _default_adapter is None:
        _default_adapter = BedrockAdapter()
    return _default_adapter


def invoke_model(prompt: str) -> dict[str, Any]:
    """Module-level invoker compatible with focus_guardian's ``invoker=`` parameter.

    Usage in the EventBridge Lambda entry point::

        import importlib
        bedrock_adapter = importlib.import_module("lambda.bedrock_adapter")
        focus_guardian = importlib.import_module("lambda.focus_guardian")

        def handler(event, context):
            return focus_guardian.lambda_handler(
                event, context, invoker=bedrock_adapter.invoke_model
            )
    """
    return _get_default_adapter().invoke_model(prompt)
