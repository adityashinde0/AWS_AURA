"""Unit tests for Server-Side Authentication & Authorization Enforcement in Project AURA.

Validates that:
- Protected endpoints reject missing credentials with HTTP 401.
- Expired and malformed JWT tokens are rejected with HTTP 401.
- Valid tokens derive caller identity from cryptographically verified claims.
- Cross-user resource access attempts are strictly rejected with HTTP 403.
- Public endpoints (CORS preflight, health checks, discovery) remain accessible.
"""
from __future__ import annotations

import importlib
import json
import time
from typing import Any
import pytest

cp = importlib.import_module("lambda.control_plane")
auth = importlib.import_module("lambda.auth")

InMemoryDataStore = cp.InMemoryDataStore
lambda_handler = cp.lambda_handler
create_test_jwt = auth.create_test_jwt
extract_and_validate_auth = auth.extract_and_validate_auth
enforce_user_authorization = auth.enforce_user_authorization
AuthError = auth.AuthError


@pytest.fixture
def store() -> InMemoryDataStore:
    return InMemoryDataStore()


# ---------------------------------------------------------------------------
# Test 1: Missing credentials on protected endpoints reject with HTTP 401
# ---------------------------------------------------------------------------

def test_missing_credentials_rejected_with_401():
    """Unauthenticated requests without token or authorizer must return 401 Unauthorized."""
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {"user_id": "usr_unauthenticated_01"},
    }
    # Invoke without store_override to enforce production auth validation
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] in ("missing_credentials", "unauthorized")


# ---------------------------------------------------------------------------
# Test 2: Expired JWT token rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_expired_jwt_token_rejected_with_401():
    """Expired Bearer tokens must be rejected with 401 Unauthorized."""
    # Create expired token (-60s in the past)
    expired_token = create_test_jwt("usr_alice", expires_in_seconds=-60.0)
    event = {
        "rawPath": "/api/v1/audit/actions",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {expired_token}"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "token_expired"


# ---------------------------------------------------------------------------
# Test 3: Malformed JWT token rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_malformed_jwt_token_rejected_with_401():
    """Malformed tokens (e.g. invalid base64 or missing segments) must return 401."""
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": "Bearer not.a.valid.jwt.token"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_token"


# ---------------------------------------------------------------------------
# Test 4: Valid Bearer JWT successfully accesses own resources
# ---------------------------------------------------------------------------

def test_valid_jwt_authorizes_own_resources(store: InMemoryDataStore):
    """Authenticated caller can access their own grants and sessions."""
    valid_token = create_test_jwt("usr_alice_authorized")
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {valid_token}"},
        "queryStringParameters": {"user_id": "usr_alice_authorized"},
    }
    resp = lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["user_id"] == "usr_alice_authorized"


# ---------------------------------------------------------------------------
# Test 5: Cross-User Access is blocked with HTTP 403 Forbidden
# ---------------------------------------------------------------------------

def test_cross_user_access_blocked_with_403(store: InMemoryDataStore):
    """Authenticated user 'usr_attacker' attempting to access 'usr_victim' resources returns 403."""
    attacker_token = create_test_jwt("usr_attacker")

    # Attacker tries to query victim's audit log
    event = {
        "rawPath": "/api/v1/audit/actions",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {attacker_token}"},
        "queryStringParameters": {"user_id": "usr_victim"},
    }
    resp = lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 403
    body = json.loads(resp["body"])
    assert body["error"] == "forbidden"
    assert "cannot access resources belonging to" in body["message"]


# ---------------------------------------------------------------------------
# Test 6: Cognito Authorizer Context integration (API Gateway HTTP API v2)
# ---------------------------------------------------------------------------

def test_cognito_authorizer_context_derives_identity(store: InMemoryDataStore):
    """Event with API Gateway Cognito authorizer context derives user_id from sub claim."""
    event = {
        "rawPath": "/api/v1/focus/bank",
        "requestContext": {
            "http": {"method": "GET"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "usr_cognito_sub_999",
                        "username": "cognito_user",
                        "custom:tenant_id": "tenant_enterprise",
                    }
                }
            },
        },
        "queryStringParameters": {"user_id": "usr_cognito_sub_999"},
    }
    resp = lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["user_id"] == "usr_cognito_sub_999"


# ---------------------------------------------------------------------------
# Test 7: Public endpoints remain accessible without authentication
# ---------------------------------------------------------------------------

def test_public_health_and_options_endpoints_accessible():
    """Health check and CORS OPTIONS preflight require zero authentication."""
    # 1. Health check
    health_resp = lambda_handler({"rawPath": "/api/v1/health", "requestContext": {"http": {"method": "GET"}}})
    assert health_resp["statusCode"] == 200
    assert json.loads(health_resp["body"])["status"] == "healthy"

    # 2. CORS OPTIONS preflight
    options_resp = lambda_handler({"rawPath": "/api/v1/consent/grants", "requestContext": {"http": {"method": "OPTIONS"}}})
    assert options_resp["statusCode"] == 200
    assert json.loads(options_resp["body"])["ok"] is True

    # 3. Discovery feature flags
    ff_resp = lambda_handler({"rawPath": "/api/v1/feature-flags", "requestContext": {"http": {"method": "GET"}}})
    assert ff_resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# Test 8: Forged and Tampered Signatures are strictly rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_forged_jwt_signature_rejected_with_401():
    """Tokens with forged/tampered cryptographic signatures must return 401 invalid_signature."""
    forged_token = create_test_jwt("usr_alice", tamper_signature=True)
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {forged_token}"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_signature"


# ---------------------------------------------------------------------------
# Test 9: Modified JWT Payload is detected and rejected
# ---------------------------------------------------------------------------

def test_modified_payload_rejected_with_401():
    """Modifying the payload segment after signing invalidates the RS256 signature."""
    valid_token = create_test_jwt("usr_alice")
    parts = valid_token.split(".")
    # Tamper payload to claim user is 'admin'
    tampered_payload = auth._b64url_encode(json.dumps({
        "sub": "admin",
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AuraTestPool",
        "aud": "aura-test-client-id",
        "token_use": "id",
        "exp": time.time() + 3600,
    }).encode("utf-8"))
    tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {tampered_token}"},
        "queryStringParameters": {"user_id": "admin"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_signature"


# ---------------------------------------------------------------------------
# Test 10: Wrong Issuer is rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_wrong_issuer_rejected_with_401(monkeypatch):
    """Tokens signed with an untrusted/mismatched issuer must be rejected."""
    monkeypatch.setenv("COGNITO_ISSUER", "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AuraTestPool")
    wrong_iss_token = create_test_jwt("usr_alice", issuer="https://untrusted-idp.attacker.com/pool_123")
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {wrong_iss_token}"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_issuer"


# ---------------------------------------------------------------------------
# Test 11: Wrong Audience is rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_wrong_audience_rejected_with_401(monkeypatch):
    """Tokens targeted for a different audience must be rejected."""
    monkeypatch.setenv("COGNITO_CLIENT_ID", "aura-test-client-id")
    wrong_aud_token = create_test_jwt("usr_alice", audience="unauthorized-client-999")
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {wrong_aud_token}"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_audience"


# ---------------------------------------------------------------------------
# Test 12: Insecure Algorithm Attacks ('none', 'HS256') are strictly blocked
# ---------------------------------------------------------------------------

def test_insecure_algorithms_strictly_rejected():
    """Attackers attempting 'none' or symmetric 'HS256' algorithm substitution must be blocked."""
    # 1. alg: "none"
    none_token = create_test_jwt("usr_alice", override_header={"alg": "none", "typ": "JWT", "kid": "aura-test-key-01"})
    event_none = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {none_token}"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp = lambda_handler(event_none)
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "invalid_algorithm"

    # 2. alg: "HS256" (symmetric key confusion attack)
    hs_token = create_test_jwt("usr_alice", override_header={"alg": "HS256", "typ": "JWT", "kid": "aura-test-key-01"})
    event_hs = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {hs_token}"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp_hs = lambda_handler(event_hs)
    assert resp_hs["statusCode"] == 401
    assert json.loads(resp_hs["body"])["error"] == "invalid_algorithm"


# ---------------------------------------------------------------------------
# Test 13: Real RSA-256 Keypair Signature Verification
# ---------------------------------------------------------------------------

def test_real_rsa_signature_verified(store: InMemoryDataStore):
    """Real RSA-signed JWT verified against registered JWKS succeeds and derives caller."""
    token = auth.create_real_rsa_test_jwt("usr_rsa_alice")
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {token}"},
        "queryStringParameters": {"user_id": "usr_rsa_alice"},
    }
    resp = lambda_handler(event, store_override=store)
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["user_id"] == "usr_rsa_alice"


# ---------------------------------------------------------------------------
# Test 14: Forged Signature is strictly rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_forged_rsa_signature_rejected_with_401():
    """Token signed with an untrusted private key or forged signature bytes is rejected."""
    # 1. Mutated signature bytes
    valid_token = auth.create_real_rsa_test_jwt("usr_rsa_alice")
    parts = valid_token.split(".")
    tampered_sig = parts[2][:-4] + ("AAAA" if not parts[2].endswith("AAAA") else "BBBB")
    forged_token = f"{parts[0]}.{parts[1]}.{tampered_sig}"

    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {forged_token}"},
        "queryStringParameters": {"user_id": "usr_rsa_alice"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    body = json.loads(resp["body"])
    assert body["error"] == "invalid_signature"

    # 2. Signed with different, untrusted RSA private key
    from cryptography.hazmat.primitives.asymmetric import rsa
    untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    untrusted_token = auth.create_real_rsa_test_jwt(
        "usr_rsa_alice", private_key=untrusted_key, kid="aura-test-key-01"
    )
    event_untrusted = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {untrusted_token}"},
        "queryStringParameters": {"user_id": "usr_rsa_alice"},
    }
    resp_untrusted = lambda_handler(event_untrusted)
    assert resp_untrusted["statusCode"] == 401
    assert json.loads(resp_untrusted["body"])["error"] == "invalid_signature"


# ---------------------------------------------------------------------------
# Test 15: Tampered Claims Payload is rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_tampered_claims_payload_rejected_with_401():
    """Attacker tampering with the claims segment without valid signature fails."""
    valid_token = auth.create_real_rsa_test_jwt("usr_rsa_alice")
    parts = valid_token.split(".")

    # Replace claims with usr_admin without valid signature
    import base64
    fake_claims = json.dumps({
        "sub": "usr_admin",
        "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_AuraUserPool",
        "aud": "aura-web-client",
        "token_use": "id",
        "exp": int(time.time() + 3600),
    }).encode("utf-8")
    tampered_payload = base64.urlsafe_b64encode(fake_claims).rstrip(b"=").decode("ascii")
    tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"

    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {tampered_token}"},
        "queryStringParameters": {"user_id": "usr_admin"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "invalid_signature"


# ---------------------------------------------------------------------------
# Test 16: Unknown Key ID (kid) is rejected with HTTP 401
# ---------------------------------------------------------------------------

def test_unknown_key_id_rejected_with_401():
    """Token with kid not present in Cognito JWKS must be rejected."""
    token = auth.create_real_rsa_test_jwt("usr_alice", kid="unknown-unregistered-kid-999")
    event = {
        "rawPath": "/api/v1/consent/grants",
        "requestContext": {"http": {"method": "GET"}},
        "headers": {"Authorization": f"Bearer {token}"},
        "queryStringParameters": {"user_id": "usr_alice"},
    }
    resp = lambda_handler(event)
    assert resp["statusCode"] == 401
    assert json.loads(resp["body"])["error"] == "key_not_found"

