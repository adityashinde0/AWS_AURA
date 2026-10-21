"""IBM watsonx.ai adapter for Project AURA Focus Guardian.

Calls the ibm-watsonx-ai text-generation REST endpoint with structured JSON
output instructions, enforces the FocusProposal contract, validates evidence
citations, resists prompt injection, and provides graceful fail-closed fallback
when the model is unavailable or evidence is insufficient.

Environment variables (all optional; defaults shown):
  WATSONX_API_KEY       – IBM Cloud IAM API key (required for live mode)
  WATSONX_PROJECT_ID    – watsonx.ai project ID (required for live mode)
  WATSONX_URL           – service endpoint, default https://us-south.ml.cloud.ibm.com
  WATSONX_MODEL_ID      – model to invoke, default ibm/granite-3-8b-instruct
  WATSONX_MOCK          – set "true" / "1" / "yes" to run in offline mock mode
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import time
from typing import Any
from urllib import request as urllib_request
from urllib.error import URLError

fg = importlib.import_module("lambda.focus_guardian")
FocusProposal: type = fg.FocusProposal
FocusRequest: type = fg.FocusRequest
safe_fallback = fg.safe_fallback

# ---------------------------------------------------------------------------
# Structured logger (mirrors focus_guardian._log contract)
# ---------------------------------------------------------------------------

def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a single structured JSON log line to stdout.

    No raw user content, evidence text, or prompt bodies appear in log fields.
    """
    record = {
        "timestamp": time.time(),
        "level": level,
        "service": "aura.watsonx_adapter",
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL_ID = "ibm/granite-3-8b-instruct"
DEFAULT_URL = "https://us-south.ml.cloud.ibm.com"
GENERATION_PATH = "/ml/v1/text/generation?version=2023-05-29"
IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"

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
# WatsonxAdapter
# ---------------------------------------------------------------------------

class WatsonxAdapter:
    """Manages IBM watsonx.ai text-generation invocation for AURA briefings."""

    def __init__(
        self,
        model_id: str | None = None,
        api_key: str | None = None,
        project_id: str | None = None,
        url: str | None = None,
        mock_mode: bool | None = None,
        raise_on_error: bool = False,
    ) -> None:
        self.model_id = model_id or os.getenv("WATSONX_MODEL_ID", DEFAULT_MODEL_ID)
        self.api_key = api_key or os.getenv("WATSONX_API_KEY", "")
        self.project_id = project_id or os.getenv("WATSONX_PROJECT_ID", "")
        self.url = (url or os.getenv("WATSONX_URL", DEFAULT_URL)).rstrip("/")
        self.mock_mode = (
            mock_mode
            if mock_mode is not None
            else os.getenv("WATSONX_MOCK", "false").lower() in ("true", "1", "yes")
        )
        self.raise_on_error = raise_on_error
        self._iam_token: str | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def generate_proposal(self, request: FocusRequest) -> FocusProposal:
        """Generate a structured FocusProposal for *request*.

        Fails closed to ``safe_fallback`` on any error.
        """
        # 1. Prompt injection scan across all evidence snippets
        for ev in request.evidence:
            snippet = ev.get("snippet") or ev.get("text") or ""
            if self._has_prompt_injection(snippet):
                _log(
                    "WARNING",
                    "watsonx_adapter_prompt_injection_blocked",
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
            if os.getenv("WATSONX_SIMULATE_ERROR"):
                if self.raise_on_error:
                    raise RuntimeError(os.getenv("WATSONX_SIMULATE_ERROR"))
                return safe_fallback(request, fallback_reason="not_implemented")
            _log(
                "INFO",
                "watsonx_adapter_mock_mode",
                user_id=request.user_id,
                session_id=request.session_id,
                evidence_count=len(valid_evidence_ids),
            )
            return self._mock_generate(request, valid_evidence_ids)

        try:
            proposal = self._invoke_watsonx(request, valid_evidence_ids)
        except Exception as exc:  # noqa: BLE001 – fail closed on any live-path error
            _log(
                "WARNING",
                "watsonx_adapter_invocation_error",
                user_id=request.user_id,
                session_id=request.session_id,
                error=type(exc).__name__,
            )
            if self.raise_on_error:
                raise
            return safe_fallback(request, fallback_reason="not_implemented")

        # 4. Hallucination guard: all cited IDs must be a subset of request evidence
        if not set(proposal.evidence_ids).issubset(set(valid_evidence_ids)):
            _log(
                "WARNING",
                "watsonx_adapter_hallucinated_evidence",
                user_id=request.user_id,
                session_id=request.session_id,
                hallucinated_count=len(set(proposal.evidence_ids) - set(valid_evidence_ids)),
            )
            return safe_fallback(request, fallback_reason="hallucinated_evidence")

        _log(
            "INFO",
            "watsonx_adapter_proposal_accepted",
            user_id=request.user_id,
            session_id=request.session_id,
            recommended_action=proposal.recommended_action,
            evidence_ids_count=len(proposal.evidence_ids),
        )
        return proposal

    # ------------------------------------------------------------------
    # Injection detection
    # ------------------------------------------------------------------

    def _has_prompt_injection(self, text: str) -> bool:
        """Return True if *text* matches any known prompt-injection pattern."""
        for pattern in PROMPT_INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    # ------------------------------------------------------------------
    # Mock path
    # ------------------------------------------------------------------

    def _mock_generate(
        self, request: FocusRequest, valid_evidence_ids: list[str]
    ) -> FocusProposal:
        """Deterministic offline mock for local development and unit tests."""
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
    # Live IBM watsonx.ai path
    # ------------------------------------------------------------------

    def _get_iam_token(self) -> str:
        """Exchange the API key for a short-lived IBM Cloud IAM bearer token."""
        if self._iam_token:
            return self._iam_token
        payload = f"grant_type=urn%3Aibm%3Aparams%3Aoauth%3Agrant-type%3Aapikey&apikey={self.api_key}"
        req = urllib_request.Request(
            IAM_TOKEN_URL,
            data=payload.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=10) as resp:
            token_data = json.loads(resp.read().decode())
        self._iam_token = token_data["access_token"]
        return self._iam_token

    def _invoke_watsonx(
        self, request: FocusRequest, valid_evidence_ids: list[str]
    ) -> FocusProposal:
        """Call the watsonx.ai text-generation endpoint and parse the response."""
        schema_def = FocusProposal.model_json_schema(by_alias=True)

        user_content = json.dumps(
            {
                "purpose": request.purpose,
                "evidence": request.evidence,
                "schema": schema_def,
            }
        )

        full_prompt = f"{SYSTEM_PROMPT}\n\nInput Data:\n{user_content}"

        payload = json.dumps(
            {
                "model_id": self.model_id,
                "project_id": self.project_id,
                "input": full_prompt,
                "parameters": {
                    "decoding_method": "greedy",
                    "max_new_tokens": 1000,
                    "temperature": 0.2,
                    "repetition_penalty": 1.05,
                },
            }
        ).encode()

        token = self._get_iam_token()
        endpoint = f"{self.url}{GENERATION_PATH}"
        req = urllib_request.Request(
            endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urllib_request.urlopen(req, timeout=15) as resp:
                response_data = json.loads(resp.read().decode())
        except URLError as exc:
            raise RuntimeError(f"watsonx.ai network error: {exc}") from exc

        raw_text: str = response_data["results"][0]["generated_text"]

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

        # Strict evidence guard (also applied in generate_proposal, belt-and-suspenders)
        if not set(proposal.evidence_ids).issubset(set(valid_evidence_ids)):
            raise ValueError("watsonx model cited evidence IDs not present in request")

        return proposal

    # ------------------------------------------------------------------
    # invoke_model — focus_guardian / gateway compatible interface
    # ------------------------------------------------------------------

    def invoke_model(self, prompt: str) -> dict[str, Any]:
        """Accept a serialised prompt string and return a raw dict.

        Matches the ``invoker=`` parameter signature expected by focus_guardian
        and resilience_gateway.
        """
        try:
            prompt_data = json.loads(prompt)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("watsonx_adapter.invoke_model received non-JSON prompt")

        evidence = prompt_data.get("evidence", [])
        purpose = prompt_data.get("purpose", "pre_meeting")
        explicit_signal = prompt_data.get("explicit_signal", "none")
        consented_score = float(prompt_data.get("consented_signal_score", 0.0))
        user_intent = prompt_data.get("user_intent")
        connected_devices = prompt_data.get("connected_devices", [])

        request = FocusRequest(
            user_id="watsonx_invoke",
            session_id=f"wx_invoke_{int(time.time())}",
            purpose=purpose,
            explicit_signal=explicit_signal,
            evidence=evidence,
            consented_signal_score=consented_score,
            user_intent=user_intent,
            connected_devices=connected_devices,
        )

        proposal = self.generate_proposal(request)
        return proposal.model_dump(by_alias=True)


# ---------------------------------------------------------------------------
# Module-level invoke_model for IBM watsonx / Granite
# ---------------------------------------------------------------------------

_default_watsonx_adapter: WatsonxAdapter | None = None


def _get_default_watsonx_adapter() -> WatsonxAdapter:
    global _default_watsonx_adapter
    if _default_watsonx_adapter is None:
        _default_watsonx_adapter = WatsonxAdapter()
    return _default_watsonx_adapter


def invoke_model(prompt: str) -> dict[str, Any]:
    """Module-level invoker compatible with focus_guardian's ``invoker=`` parameter."""
    return _get_default_watsonx_adapter().invoke_model(prompt)
