"""Enterprise Multi-Provider Resilience Layer for Project AURA.

Provides a MultiProviderModelGateway that invokes a primary provider (AWS Bedrock)
and automatically fails over to a secondary provider (IBM Granite / watsonx) when
the primary returns a timeout, throttling (429), or 5xx error.

Structured log fields per invocation:
  correlation_id, provider_used, latency_ms, failover_triggered
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

# ---------------------------------------------------------------------------
# Structured logger (mirrors the pattern from focus_guardian.py)
# ---------------------------------------------------------------------------

def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a single structured JSON log line to stdout.

    No raw prompt bodies, auth tokens, or PII are ever included in log fields.
    """
    record: dict[str, Any] = {
        "timestamp": time.time(),
        "level": level,
        "service": "aura.resilience_gateway",
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base class for provider invocation failures."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderThrottleError(ProviderError):
    """Raised when the provider returns HTTP 429 (TooManyRequests)."""


class ProviderTimeoutError(ProviderError):
    """Raised when the provider call exceeds the configured deadline."""


class ProviderServerError(ProviderError):
    """Raised when the provider returns a 5xx status code."""


class AllProvidersUnavailableError(Exception):
    """Raised when every configured provider fails and no fallback is possible."""


# ---------------------------------------------------------------------------
# Provider interface / adapters
# ---------------------------------------------------------------------------

ProviderName = Literal["bedrock", "watsonx", "stub"]


@dataclass
class InvocationResult:
    """Structured response from a single model invocation attempt."""

    provider: ProviderName
    payload: dict[str, Any]
    latency_ms: float
    failover_triggered: bool
    correlation_id: str


@dataclass
class ProviderConfig:
    """Configuration for a single provider endpoint."""

    name: ProviderName
    timeout_seconds: float = 10.0
    max_retries: int = 0  # retries *within* this provider before failing over
    # Callable injected at construction time (allows easy monkeypatching in tests)
    _invoke: Callable[[str, str], dict[str, Any]] | None = field(default=None, repr=False)

    def invoke(self, prompt: str, model_id: str) -> dict[str, Any]:
        """Call the underlying provider; raise ProviderError subclasses on failure."""
        if self._invoke is not None:
            return self._invoke(prompt, model_id)
        raise NotImplementedError(
            f"No invoke callable wired for provider '{self.name}'. "
            "Supply a real adapter in production or a stub in tests."
        )


# ---------------------------------------------------------------------------
# Built-in stub adapters (used for local tests / hackathon demo)
# ---------------------------------------------------------------------------

def _bedrock_stub(prompt: str, model_id: str) -> dict[str, Any]:  # pragma: no cover
    """Placeholder that raises NotImplementedError in environments without boto3."""
    raise NotImplementedError("AWS Bedrock adapter not configured; wire a real boto3 client in production.")


def _watsonx_stub(prompt: str, model_id: str) -> dict[str, Any]:  # pragma: no cover
    """Placeholder that raises NotImplementedError in environments without ibm-watson-machine-learning."""
    raise NotImplementedError("IBM watsonx adapter not configured; wire the ibm-generative-ai SDK in production.")


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class MultiProviderModelGateway:
    """Attempt primary model invocation (Bedrock) and fail over to IBM Granite/watsonx.

    Failover triggers on:
      - ProviderTimeoutError  (deadline exceeded)
      - ProviderThrottleError (HTTP 429)
      - ProviderServerError   (HTTP 5xx)
      - NotImplementedError   (adapter not wired — treated as unavailable)

    Every invocation emits a structured log line that includes:
      correlation_id, provider_used, latency_ms, failover_triggered
    """

    #: Default Bedrock model to use when none is specified.
    DEFAULT_BEDROCK_MODEL = "amazon.nova-pro-v1:0"
    #: Default watsonx / Granite model to use when none is specified.
    DEFAULT_WATSONX_MODEL = "ibm/granite-13b-instruct-v2"

    def __init__(
        self,
        primary: ProviderConfig | None = None,
        secondary: ProviderConfig | None = None,
        bedrock_model_id: str = DEFAULT_BEDROCK_MODEL,
        watsonx_model_id: str = DEFAULT_WATSONX_MODEL,
    ) -> None:
        self.primary: ProviderConfig = primary or ProviderConfig(
            name="bedrock", _invoke=_bedrock_stub
        )
        self.secondary: ProviderConfig = secondary or ProviderConfig(
            name="watsonx", _invoke=_watsonx_stub
        )
        self.bedrock_model_id = bedrock_model_id
        self.watsonx_model_id = watsonx_model_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def invoke(
        self,
        prompt: str,
        correlation_id: str | None = None,
    ) -> InvocationResult:
        """Invoke primary provider; fall over to secondary on transient failures.

        Parameters
        ----------
        prompt:
            The fully-formed model prompt string (JSON or plain text).
        correlation_id:
            Optional caller-supplied trace ID.  A UUID is generated if omitted.

        Returns
        -------
        InvocationResult
            Structured result with provider name, payload, latency, and failover flag.

        Raises
        ------
        AllProvidersUnavailableError
            If both primary and secondary fail.
        """
        cid = correlation_id or str(uuid.uuid4())
        failover_triggered = False

        # --- Primary attempt ---
        primary_error: Exception | None = None
        t0 = time.monotonic()
        try:
            payload = self._invoke_with_retries(self.primary, prompt, self.bedrock_model_id)
            latency_ms = (time.monotonic() - t0) * 1000
            result = InvocationResult(
                provider=self.primary.name,
                payload=payload,
                latency_ms=latency_ms,
                failover_triggered=False,
                correlation_id=cid,
            )
            self._log_invocation(result)
            return result
        except (ProviderError, NotImplementedError, Exception) as exc:
            primary_error = exc
            latency_ms = (time.monotonic() - t0) * 1000
            _log(
                "WARNING",
                "resilience_gateway_primary_failed",
                correlation_id=cid,
                provider=self.primary.name,
                error_type=type(exc).__name__,
                latency_ms=round(latency_ms, 2),
            )
            failover_triggered = True

        # --- Secondary (failover) attempt ---
        t1 = time.monotonic()
        try:
            payload = self._invoke_with_retries(self.secondary, prompt, self.watsonx_model_id)
            latency_ms = (time.monotonic() - t1) * 1000
            result = InvocationResult(
                provider=self.secondary.name,
                payload=payload,
                latency_ms=latency_ms,
                failover_triggered=True,
                correlation_id=cid,
            )
            self._log_invocation(result)
            return result
        except (ProviderError, NotImplementedError, Exception) as secondary_error:
            latency_ms = (time.monotonic() - t1) * 1000
            _log(
                "ERROR",
                "resilience_gateway_all_providers_failed",
                correlation_id=cid,
                primary_error=str(primary_error),
                secondary_error=str(secondary_error),
                latency_ms=round(latency_ms, 2),
            )
            raise AllProvidersUnavailableError(
                f"Both providers failed. Primary: {primary_error!s}. Secondary: {secondary_error!s}."
            ) from secondary_error

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _invoke_with_retries(
        self,
        provider: ProviderConfig,
        prompt: str,
        model_id: str,
    ) -> dict[str, Any]:
        """Run provider.invoke() up to (max_retries+1) times on retriable errors.

        Wall-clock timeout (provider.timeout_seconds) is strictly enforced using ThreadPoolExecutor.
        Permanent configuration and authentication errors fail immediately without retrying.
        """
        last_exc: Exception | None = None
        attempts = max(1, provider.max_retries + 1)
        for attempt in range(attempts):
            try:
                # Genuinely enforce wall-clock timeout without blocking on thread cleanup
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    future = executor.submit(provider.invoke, prompt, model_id)
                    return future.result(timeout=provider.timeout_seconds)
                except concurrent.futures.TimeoutError as te:
                    raise ProviderTimeoutError(
                        f"Provider '{provider.name}' exceeded timeout of {provider.timeout_seconds}s"
                    ) from te
                finally:
                    executor.shutdown(wait=False, cancel_futures=True)
            except (ProviderThrottleError, ProviderTimeoutError, ProviderServerError) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    _log(
                        "INFO",
                        "resilience_gateway_retry",
                        provider=provider.name,
                        attempt=attempt + 1,
                        error_type=type(exc).__name__,
                    )
            except (ProviderError, NotImplementedError) as exc:
                # Permanent, auth, or unconfigured errors fail immediately without retrying
                raise exc
            except (ValueError, TypeError, KeyError) as exc:
                # Format or schema parsing errors are deterministic and should fail fast
                raise ProviderError(f"Provider invocation format error: {exc}") from exc
            except Exception as exc:
                last_exc = ProviderServerError(f"Unexpected provider error: {exc}")
                if attempt < attempts - 1:
                    _log(
                        "INFO",
                        "resilience_gateway_retry",
                        provider=provider.name,
                        attempt=attempt + 1,
                        error_type=type(exc).__name__,
                    )
        # All retries exhausted — re-raise the last exception
        assert last_exc is not None
        raise last_exc

    def _log_invocation(self, result: InvocationResult) -> None:
        """Emit the canonical per-invocation structured log line."""
        _log(
            "INFO",
            "resilience_gateway_invocation_complete",
            correlation_id=result.correlation_id,
            provider_used=result.provider,
            latency_ms=round(result.latency_ms, 2),
            failover_triggered=result.failover_triggered,
        )


# ---------------------------------------------------------------------------
# Real adapter factories — wires real adapters for Bedrock and watsonx
# ---------------------------------------------------------------------------

def create_real_bedrock_invoker() -> Callable[[str, str], dict[str, Any]]:
    """Factory returning an invoke callable wired to lambda.bedrock_adapter.BedrockAdapter."""
    import importlib
    ba = importlib.import_module("lambda.bedrock_adapter")

    def _invoke(prompt: str, model_id: str) -> dict[str, Any]:
        adapter = ba.BedrockAdapter(model_id=model_id, raise_on_error=True)
        try:
            return adapter.invoke_model(prompt)
        except Exception as exc:
            exc_name = type(exc).__name__
            msg = str(exc)
            if "Throttle" in exc_name or "TooManyRequests" in msg or "429" in msg:
                raise ProviderThrottleError(f"Bedrock throttled: {msg}", status_code=429) from exc
            if "Timeout" in exc_name or "ReadTimeout" in msg:
                raise ProviderTimeoutError(f"Bedrock timeout: {msg}") from exc
            if "Server" in exc_name or "500" in msg or "503" in msg:
                raise ProviderServerError(f"Bedrock 5xx error: {msg}", status_code=500) from exc
            if isinstance(exc, (NotImplementedError, RuntimeError)):
                raise NotImplementedError(msg) from exc
            raise ProviderError(f"Bedrock provider error: {msg}") from exc

    return _invoke


def create_real_watsonx_invoker() -> Callable[[str, str], dict[str, Any]]:
    """Factory returning an invoke callable wired to lambda.watsonx_adapter.WatsonxAdapter."""
    import importlib
    wa = importlib.import_module("lambda.watsonx_adapter")

    def _invoke(prompt: str, model_id: str) -> dict[str, Any]:
        adapter = wa.WatsonxAdapter(model_id=model_id, raise_on_error=True)
        if not adapter.api_key and not adapter.mock_mode:
            import os
            secret_arn = os.getenv("WATSONX_SECRET_ARN")
            if secret_arn:
                try:
                    import boto3
                    client = boto3.client("secretsmanager")
                    secret_val = client.get_secret_value(SecretId=secret_arn)
                    sec_dict = json.loads(secret_val.get("SecretString", "{}"))
                    adapter.api_key = sec_dict.get("WATSONX_API_KEY", "")
                    adapter.project_id = sec_dict.get("WATSONX_PROJECT_ID", adapter.project_id)
                except Exception as sec_exc:
                    _log("WARNING", "watsonx_secret_fetch_failed", error=str(sec_exc))
            if not adapter.api_key and not adapter.mock_mode:
                raise NotImplementedError("IBM watsonx credentials (WATSONX_API_KEY or WATSONX_SECRET_ARN) not configured")
        try:
            return adapter.invoke_model(prompt)
        except Exception as exc:
            exc_name = type(exc).__name__
            msg = str(exc)
            if "Throttle" in exc_name or "429" in msg:
                raise ProviderThrottleError(f"watsonx throttled: {msg}", status_code=429) from exc
            if "Timeout" in exc_name or "ReadTimeout" in msg:
                raise ProviderTimeoutError(f"watsonx timeout: {msg}") from exc
            if "500" in msg or "502" in msg or "503" in msg or "ServerError" in exc_name:
                raise ProviderServerError(f"watsonx 5xx error: {msg}", status_code=500) from exc
            if "401" in msg or "403" in msg or "Unauthorized" in msg:
                raise ProviderError(f"watsonx authentication failed: {msg}", status_code=401) from exc
            if isinstance(exc, (NotImplementedError, RuntimeError)):
                raise NotImplementedError(msg) from exc
            raise ProviderError(f"watsonx provider error: {msg}") from exc

    return _invoke


# ---------------------------------------------------------------------------
# Convenience factory — wires a ready-to-use gateway for the Lambda runtime
# ---------------------------------------------------------------------------

def make_gateway(
    bedrock_invoke: Callable[[str, str], dict[str, Any]] | None = None,
    watsonx_invoke: Callable[[str, str], dict[str, Any]] | None = None,
    primary_timeout: float = 5.0,
    secondary_timeout: float = 8.0,
    primary_retries: int = 1,
    wire_real_adapters: bool = False,
) -> MultiProviderModelGateway:
    """Return a configured gateway suitable for Lambda injection.

    Parameters
    ----------
    bedrock_invoke:
        Callable ``(prompt, model_id) -> dict`` wrapping the real boto3
        ``bedrock-runtime`` client. Defaults to the stub unless wire_real_adapters=True.
    watsonx_invoke:
        Callable wrapping the IBM Generative AI SDK. Defaults to the stub unless wire_real_adapters=True.
    primary_timeout, secondary_timeout:
        Per-provider wall-clock deadline in seconds (strictly enforced via ThreadPoolExecutor).
        Default 5.0s primary + 8.0s secondary fits comfortably inside Lambda 30s timeout.
    primary_retries:
        Number of additional attempts for the primary provider before
        triggering failover.
    wire_real_adapters:
        When True, automatically wires to BedrockAdapter and WatsonxAdapter.
    """
    if wire_real_adapters:
        bedrock_callable = bedrock_invoke or create_real_bedrock_invoker()
        watsonx_callable = watsonx_invoke or create_real_watsonx_invoker()
    else:
        bedrock_callable = bedrock_invoke or _bedrock_stub
        watsonx_callable = watsonx_invoke or _watsonx_stub

    primary = ProviderConfig(
        name="bedrock",
        timeout_seconds=primary_timeout,
        max_retries=primary_retries,
        _invoke=bedrock_callable,
    )
    secondary = ProviderConfig(
        name="watsonx",
        timeout_seconds=secondary_timeout,
        max_retries=0,
        _invoke=watsonx_callable,
    )
    return MultiProviderModelGateway(primary=primary, secondary=secondary)


def make_production_gateway(
    primary_timeout: float = 5.0,
    secondary_timeout: float = 8.0,
    primary_retries: int = 1,
) -> MultiProviderModelGateway:
    """Convenience factory returning a production-ready gateway wired to real adapters."""
    return make_gateway(
        primary_timeout=primary_timeout,
        secondary_timeout=secondary_timeout,
        primary_retries=primary_retries,
        wire_real_adapters=True,
    )
