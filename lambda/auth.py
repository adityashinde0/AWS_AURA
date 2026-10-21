"""AURA Enterprise Authentication & Authorization Module.

Enforces server-side authentication token validation and resource ownership
for all Project AURA HTTP endpoints. Eliminates reliance on untrusted client-supplied
user IDs by deriving identity strictly from cryptographically verified claims.

Supported Authorization Patterns:
1. API Gateway HTTP API Cognito JWT Authorizer:
   event["requestContext"]["authorizer"]["jwt"]["claims"]
   - Validated by API Gateway at the edge against Amazon Cognito JWKS.
   - Verified for required claims ('sub'), expiration, issuer, audience, and token_use.
2. Direct Authorization Header (Bearer JWT):
   - Cryptographically verified against Cognito JWKS using RSA-SHA256 (RS256).
   - Validates header algorithm ('RS256' strictly required; 'none' and symmetric rejected).
   - Validates key identifier ('kid') against cached/refreshed JWKS keys.
   - Enforces cryptographic signature verification using public key.
   - Validates standard claims: 'sub', 'exp' (clock expiration), 'iss' (issuer),
     'aud' / 'client_id' (audience), and 'token_use'.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal

try:
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.exceptions import InvalidSignature
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------

def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a structured JSON log line. No secrets, tokens, or PII logged."""
    record = {
        "timestamp": time.time(),
        "level": level,
        "service": "aura.auth",
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Auth Errors
# ---------------------------------------------------------------------------

class AuthError(Exception):
    """Raised when authentication or authorization fails."""

    def __init__(self, status_code: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message


# ---------------------------------------------------------------------------
# Auth Context
# ---------------------------------------------------------------------------

@dataclass
class AuthContext:
    """Verified identity context derived from authorizer claims or Bearer token."""

    user_id: str
    tenant_id: str = "tenant_default"
    claims: dict[str, Any] = field(default_factory=dict)
    is_authenticated: bool = True
    auth_method: Literal["cognito_jwt", "bearer_jwt", "test_harness", "anonymous"] = "cognito_jwt"
    error: str | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Public Routes Specification
# ---------------------------------------------------------------------------

PUBLIC_ROUTES: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/api/v1/health"),
    ("OPTIONS", "*"),  # CORS preflight is always public
    ("GET", "/api/v1/feature-flags"),  # System feature flags discovery
    ("GET", "/api/v1/devices"),        # Public certified device discovery
    ("POST", "/api/v1/slack/oauth/callback"),  # OAuth callback redirect from Slack
}


def is_public_route(http_method: str, path: str) -> bool:
    """Return True if the route is explicitly declared public."""
    if http_method.upper() == "OPTIONS":
        return True
    if (http_method.upper(), path) in PUBLIC_ROUTES:
        return True
    return False


# ---------------------------------------------------------------------------
# Base64URL Encoding & Decoding
# ---------------------------------------------------------------------------

def _b64url_decode(segment: str) -> bytes:
    """Decode base64url encoded string with proper padding."""
    rem = len(segment) % 4
    if rem > 0:
        segment += "=" * (4 - rem)
    return base64.urlsafe_b64decode(segment.encode("ascii"))


def _b64url_encode(data: bytes) -> str:
    """Encode bytes to base64url without trailing '=' padding."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# JWKS & Public Key Management
# ---------------------------------------------------------------------------

class JWKSManager:
    """Manages RSA public keys for cryptographic JWT verification.

    Supports:
    - In-memory registered keys (for unit and integration tests)
    - AWS Cognito JWKS URL retrieval with caching and rotation
    """

    def __init__(self) -> None:
        self._keys: dict[str, Any] = {}
        self._last_fetched: float = 0.0
        self._cache_ttl_seconds: float = 300.0  # 5 minutes

    def register_public_key(self, kid: str, public_key: Any) -> None:
        """Register an RSA public key object directly (e.g. for testing)."""
        self._keys[kid] = public_key

    def register_jwk(self, jwk: dict[str, Any]) -> None:
        """Parse and register a single JWK dict with kty='RSA'."""
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography package required for JWK parsing")
        kid = jwk.get("kid")
        if not kid or jwk.get("kty") != "RSA":
            return
        n_str = jwk.get("n", "")
        e_str = jwk.get("e", "")
        if not n_str or not e_str:
            return
        n_int = int.from_bytes(_b64url_decode(n_str), "big")
        e_int = int.from_bytes(_b64url_decode(e_str), "big")
        pub_key = rsa.RSAPublicNumbers(e_int, n_int).public_key()
        self._keys[kid] = pub_key

    def register_jwks(self, jwks: dict[str, Any]) -> None:
        """Register all keys in a JWKS dictionary."""
        for key_dict in jwks.get("keys", []):
            self.register_jwk(key_dict)

    def clear(self) -> None:
        """Clear all registered keys."""
        self._keys.clear()
        self._last_fetched = 0.0

    def get_public_key(self, kid: str) -> Any | None:
        """Retrieve the public key for kid, refreshing JWKS if configured and missing."""
        if kid in self._keys:
            return self._keys[kid]

        # Attempt to fetch from Cognito if configured
        jwks_url = self._get_cognito_jwks_url()
        now = time.time()
        if jwks_url and (now - self._last_fetched > 30.0):
            try:
                self._fetch_remote_jwks(jwks_url)
                self._last_fetched = now
            except Exception as exc:
                _log("WARNING", "jwks_fetch_failed", error=str(exc), url=jwks_url)

        return self._keys.get(kid)

    def _get_cognito_jwks_url(self) -> str | None:
        custom_url = os.getenv("COGNITO_JWKS_URL")
        if custom_url:
            return custom_url
        pool_id = os.getenv("COGNITO_USER_POOL_ID")
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        if pool_id:
            return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
        return None

    def _fetch_remote_jwks(self, url: str) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "AURA-Auth/1.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.register_jwks(data)


# Global JWKS manager instance
jwks_manager = JWKSManager()


# ---------------------------------------------------------------------------
# Cryptographic JWT Validation
# ---------------------------------------------------------------------------

def verify_and_decode_bearer_jwt(
    token: str,
    expected_issuer: str | None = None,
    expected_audience: str | None = None,
    key_manager: JWKSManager | None = None,
) -> dict[str, Any]:
    """Cryptographically verify a Bearer JWT signature and validate standard claims.

    Enforces:
    1. Exactly 3 dot-separated segments.
    2. Header parsing and strict RS256 algorithm enforcement.
    3. 'kid' key resolution from registered JWKS.
    4. RSA PKCS1v15 SHA-256 cryptographic signature verification.
    5. Valid JSON payload with 'sub' claim.
    6. Expiration check ('exp').
    7. Issuer check ('iss') against Cognito pool if specified.
    8. Audience / Client ID check ('aud' or 'client_id') if specified.
    9. Token use check ('token_use') - must be 'id' or 'access'.
    """
    if not _CRYPTO_AVAILABLE:
        raise AuthError(500, "crypto_unavailable", "Server cryptography library is unavailable")

    parts = token.strip().split(".")
    if len(parts) != 3:
        raise AuthError(401, "invalid_token", "JWT token must contain exactly 3 segments")

    # 1. Parse header
    try:
        header_bytes = _b64url_decode(parts[0])
        header = json.loads(header_bytes.decode("utf-8"))
    except Exception as exc:
        raise AuthError(401, "invalid_token", f"Malformed JWT header: {str(exc)}") from exc

    if not isinstance(header, dict):
        raise AuthError(401, "invalid_token", "JWT header must be a JSON object")

    # Algorithm check: strictly RS256 (no 'none', no HMAC symmetric)
    alg = header.get("alg")
    if alg != "RS256":
        raise AuthError(
            401,
            "invalid_algorithm",
            f"Unsupported algorithm '{alg}'. Only 'RS256' is permitted.",
        )

    # 2. Key lookup via 'kid'
    kid = header.get("kid")
    if not kid:
        raise AuthError(401, "missing_kid", "JWT header missing required 'kid' field")

    km = key_manager or jwks_manager
    public_key = km.get_public_key(kid)
    if not public_key:
        raise AuthError(401, "key_not_found", f"Signing key '{kid}' not found in registered JWKS")

    # 3. Cryptographic Signature Verification
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    try:
        signature_bytes = _b64url_decode(parts[2])
    except Exception as exc:
        raise AuthError(401, "invalid_signature", f"Malformed base64url signature: {str(exc)}") from exc

    try:
        public_key.verify(signature_bytes, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        _log("WARNING", "jwt_signature_verification_failed", kid=kid)
        raise AuthError(401, "invalid_signature", "Cryptographic signature verification failed") from exc
    except Exception as exc:
        raise AuthError(401, "invalid_signature", f"Signature verification error: {str(exc)}") from exc

    # 4. Parse payload
    try:
        payload_bytes = _b64url_decode(parts[1])
        claims = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise AuthError(401, "invalid_token", f"Failed to parse JWT payload: {str(exc)}") from exc

    if not isinstance(claims, dict):
        raise AuthError(401, "invalid_token", "JWT payload must be a valid JSON object")

    now = time.time()

    # 5. Expiration check
    exp = claims.get("exp")
    if exp is None:
        raise AuthError(401, "missing_exp", "JWT missing required 'exp' claim")
    try:
        exp_val = float(exp)
        if now > exp_val:
            raise AuthError(401, "token_expired", f"Token expired at {exp_val} (current time {now})")
    except (ValueError, TypeError) as exc:
        raise AuthError(401, "invalid_token", "Malformed 'exp' claim in JWT") from exc

    # 6. Not Before check ('nbf') if present
    nbf = claims.get("nbf")
    if nbf is not None:
        try:
            if now < float(nbf):
                raise AuthError(401, "token_not_yet_valid", "Token not valid before nbf claim")
        except (ValueError, TypeError):
            pass

    # 7. Identity claim presence
    sub = claims.get("sub") or claims.get("cognito:username") or claims.get("username")
    if not sub:
        raise AuthError(401, "invalid_token", "JWT missing required 'sub' or 'username' claim")

    # 8. Token use check ('token_use')
    token_use = claims.get("token_use")
    if token_use and token_use not in ("id", "access"):
        raise AuthError(401, "invalid_token_use", f"Invalid token_use '{token_use}'; must be 'id' or 'access'")

    # 9. Issuer check ('iss')
    iss = claims.get("iss")
    expected_iss = expected_issuer or _resolve_expected_issuer()
    if expected_iss and iss != expected_iss:
        raise AuthError(401, "invalid_issuer", f"Token issuer '{iss}' does not match expected '{expected_iss}'")

    # 10. Audience check ('aud' or 'client_id')
    expected_aud = expected_audience or os.getenv("COGNITO_CLIENT_ID")
    if expected_aud:
        token_aud = claims.get("aud") or claims.get("client_id")
        if token_aud != expected_aud:
            raise AuthError(401, "invalid_audience", f"Token audience '{token_aud}' does not match expected '{expected_aud}'")

    return claims


def _resolve_expected_issuer() -> str | None:
    iss = os.getenv("COGNITO_ISSUER")
    if iss:
        return iss
    pool_id = os.getenv("COGNITO_USER_POOL_ID")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
    if pool_id:
        return f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
    return None


# ---------------------------------------------------------------------------
# Core Authenticator
# ---------------------------------------------------------------------------

def extract_and_validate_auth(
    event: dict[str, Any],
    key_manager: JWKSManager | None = None,
    allow_test_bypass: bool = False,
) -> AuthContext:
    """Extract and authenticate the caller from the API Gateway event envelope.

    Verification Hierarchy:
    1. API Gateway HTTP API Cognito JWT Authorizer:
       event["requestContext"]["authorizer"]["jwt"]["claims"]
       (Pre-validated at the AWS API Gateway edge against Cognito JWKS).
    2. API Gateway REST / Custom authorizer:
       event["requestContext"]["authorizer"]["claims"]
    3. Direct Authorization Header (Bearer JWT):
       Cryptographically verified against registered JWKS using RS256.
       Unsigned, incorrectly signed, or forged tokens are strictly rejected.
    4. Controlled Test Bypass:
       Only permitted when allow_test_bypass=True AND strict auth is not enforced,
       and never in live AWS Lambda.
    """
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}

    # 1. API Gateway HTTP API v2 Cognito Authorizer Context
    jwt_ctx = authorizer.get("jwt") or {}
    claims = jwt_ctx.get("claims") or authorizer.get("claims")

    if claims and isinstance(claims, dict):
        user_id = claims.get("sub") or claims.get("cognito:username") or claims.get("username")
        tenant_id = claims.get("custom:tenant_id") or claims.get("tenant_id") or "tenant_default"

        # Check expiration if present in authorizer claims
        exp = claims.get("exp")
        if exp is not None:
            try:
                if time.time() > float(exp):
                    return AuthContext(
                        user_id="",
                        is_authenticated=False,
                        error="token_expired",
                        error_message="Authorizer token has expired",
                    )
            except (ValueError, TypeError):
                pass

        if user_id:
            return AuthContext(
                user_id=str(user_id),
                tenant_id=str(tenant_id),
                claims=claims,
                is_authenticated=True,
                auth_method="cognito_jwt",
            )

    # 2. HTTP Authorization Header (Bearer JWT verification)
    headers = event.get("headers") or {}
    auth_header = ""
    for k, v in headers.items():
        if k.lower() == "authorization" and isinstance(v, str):
            auth_header = v.strip()
            break

    if auth_header:
        if not auth_header.lower().startswith("bearer "):
            return AuthContext(
                user_id="",
                is_authenticated=False,
                error="invalid_authorization_scheme",
                error_message="Authorization header must use Bearer scheme",
            )

        token = auth_header[7:].strip()
        try:
            parsed_claims = verify_and_decode_bearer_jwt(
                token,
                key_manager=key_manager,
            )
            user_id = str(parsed_claims.get("sub") or parsed_claims.get("username"))
            tenant_id = str(parsed_claims.get("custom:tenant_id") or parsed_claims.get("tenant_id") or "tenant_default")
            return AuthContext(
                user_id=user_id,
                tenant_id=tenant_id,
                claims=parsed_claims,
                is_authenticated=True,
                auth_method="bearer_jwt",
            )
        except AuthError as err:
            return AuthContext(
                user_id="",
                is_authenticated=False,
                error=err.error,
                error_message=err.message,
            )

    strict_auth = os.getenv("AURA_STRICT_AUTH", "").strip().lower() in ("true", "1", "yes")
    is_live_aws = bool(
        os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        or os.getenv("LAMBDA_TASK_ROOT")
        or os.getenv("AWS_EXECUTION_ENV")
        or os.getenv("AWS_LAMBDA_RUNTIME_API")
    )

    if allow_test_bypass and not strict_auth and not is_live_aws:
        query_params = event.get("queryStringParameters") or {}
        test_user = query_params.get("user_id") or ""
        test_tenant = query_params.get("tenant_id") or ""
        if not test_user or not test_tenant:
            raw_body = event.get("body")
            if isinstance(raw_body, str):
                try:
                    parsed_body = json.loads(raw_body)
                    if isinstance(parsed_body, dict):
                        if not test_user:
                            test_user = parsed_body.get("user_id", "")
                        if not test_tenant:
                            test_tenant = parsed_body.get("tenant_id", "")
                except Exception:
                    pass
            elif isinstance(raw_body, dict):
                if not test_user:
                    test_user = raw_body.get("user_id", "")
                if not test_tenant:
                    test_tenant = raw_body.get("tenant_id", "")

        if not test_user:
            test_user = "usr_marcus_test"
        if not test_tenant:
            test_tenant = "tenant_default"

        if test_user:
            return AuthContext(
                user_id=str(test_user),
                tenant_id=str(test_tenant),
                claims={"sub": str(test_user), "tenant_id": str(test_tenant)},
                is_authenticated=True,
                auth_method="test_harness",
            )

    # 4. Missing credentials
    return AuthContext(
        user_id="",
        is_authenticated=False,
        error="missing_credentials",
        error_message="Authorization token required for protected endpoint",
    )


# ---------------------------------------------------------------------------
# Authorization & Ownership Enforcement
# ---------------------------------------------------------------------------

def enforce_user_authorization(
    auth: AuthContext,
    target_user_id: str | None = None,
) -> str:
    """Enforce that the caller is authenticated and authorized to access target_user_id.

    Returns the verified user_id to be used by all downstream operations.
    Raises AuthError with:
    - HTTP 401 if not authenticated.
    - HTTP 403 if authenticated caller attempts cross-user access.
    """
    if not auth.is_authenticated or not auth.user_id:
        raise AuthError(
            401,
            auth.error or "unauthorized",
            auth.error_message or "Missing or invalid authentication credentials",
        )

    # Resource ownership check
    if target_user_id and target_user_id.strip():
        if auth.user_id != target_user_id.strip():
            _log(
                "WARNING",
                "cross_user_access_blocked",
                caller_user_id=auth.user_id,
                target_user_id=target_user_id,
            )
            raise AuthError(
                403,
                "forbidden",
                f"Access denied: authenticated caller '{auth.user_id}' cannot access resources belonging to '{target_user_id}'",
            )

    return auth.user_id


def enforce_tenant_authorization(
    auth: AuthContext,
    target_tenant_id: str | None = None,
) -> str:
    """Enforce that the caller is authenticated and authorized for the target tenant.

    Returns the verified tenant_id to be used by all downstream operations.
    Raises AuthError with:
    - HTTP 401 if not authenticated.
    - HTTP 403 if authenticated caller attempts cross-tenant access.
    """
    if not auth.is_authenticated or not auth.user_id:
        raise AuthError(
            401,
            auth.error or "unauthorized",
            auth.error_message or "Missing or invalid authentication credentials",
        )

    effective_tenant = auth.tenant_id or "tenant_default"
    if target_tenant_id and target_tenant_id.strip():
        req_tenant = target_tenant_id.strip()
        if req_tenant != effective_tenant:
            _log(
                "WARNING",
                "cross_tenant_access_blocked",
                caller_tenant_id=effective_tenant,
                target_tenant_id=req_tenant,
            )
            raise AuthError(
                403,
                "forbidden",
                f"Access denied: authenticated caller in tenant '{effective_tenant}' cannot access resources belonging to tenant '{req_tenant}'",
            )

    return effective_tenant


# ---------------------------------------------------------------------------
# Cryptographic Test Helper: Real RSA Signed Token Generator
# ---------------------------------------------------------------------------

_DEFAULT_TEST_KEY_ID = "aura-test-key-01"
_DEFAULT_TEST_PRIVATE_KEY: Any = None


def _get_default_test_private_key() -> Any:
    """Lazily generate and register an in-memory 2048-bit RSA test key pair."""
    global _DEFAULT_TEST_PRIVATE_KEY
    if _DEFAULT_TEST_PRIVATE_KEY is None:
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography package required for RSA test key generation")
        _DEFAULT_TEST_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pub_key = _DEFAULT_TEST_PRIVATE_KEY.public_key()
        jwks_manager.register_public_key(_DEFAULT_TEST_KEY_ID, pub_key)
    return _DEFAULT_TEST_PRIVATE_KEY


def create_real_rsa_test_jwt(
    user_id: str,
    private_key: Any | None = None,
    kid: str = "aura-test-key-01",
    tenant_id: str = "tenant_default",
    expires_in_seconds: float = 3600.0,
    issuer: str = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AuraTestPool",
    audience: str = "aura-test-client-id",
    token_use: str = "id",
    extra_claims: dict[str, Any] | None = None,
    tamper_signature: bool = False,
    override_header: dict[str, Any] | None = None,
) -> str:
    """Generate a genuinely RSA-signed JWT token using RS256 for unit testing."""
    now = time.time()
    header = override_header or {"alg": "RS256", "typ": "JWT", "kid": kid}
    claims = {
        "sub": user_id,
        "cognito:username": user_id,
        "custom:tenant_id": tenant_id,
        "iss": issuer,
        "aud": audience,
        "token_use": token_use,
        "auth_time": int(now),
        "iat": int(now),
        "exp": int(now + expires_in_seconds),
    }
    if extra_claims:
        claims.update(extra_claims)

    h_str = _b64url_encode(json.dumps(header).encode("utf-8"))
    c_str = _b64url_encode(json.dumps(claims).encode("utf-8"))
    signing_input = f"{h_str}.{c_str}".encode("ascii")

    key = private_key or _get_default_test_private_key()
    if tamper_signature:
        sig_str = _b64url_encode(b"tampered_invalid_signature_bytes_999")
    else:
        sig_bytes = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        sig_str = _b64url_encode(sig_bytes)

    return f"{h_str}.{c_str}.{sig_str}"


def create_test_jwt(
    user_id: str,
    tenant_id: str = "tenant_default",
    expires_in_seconds: float = 3600.0,
    issuer: str = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AuraTestPool",
    audience: str = "aura-test-client-id",
    token_use: str = "id",
    extra_claims: dict[str, Any] | None = None,
    tamper_signature: bool = False,
    override_header: dict[str, Any] | None = None,
) -> str:
    """Convenience helper: generates an RS256 cryptographically signed test JWT."""
    priv_key = _get_default_test_private_key()
    return create_real_rsa_test_jwt(
        user_id=user_id,
        private_key=priv_key,
        kid=_DEFAULT_TEST_KEY_ID,
        tenant_id=tenant_id,
        expires_in_seconds=expires_in_seconds,
        issuer=issuer,
        audience=audience,
        token_use=token_use,
        extra_claims=extra_claims,
        tamper_signature=tamper_signature,
        override_header=override_header,
    )

