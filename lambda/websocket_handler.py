"""WebSocket Handler for Project AURA Cloud Intake.

Handles API Gateway WebSocket routes:
- $connect: Authenticates connection, registers in DynamoDB with TTL.
- $disconnect: Cleans up connection state.
- relay: Ingests sanitized client envelopes, validates schema and privacy boundaries,
  emits event to EventBridge, and returns an immediate ACK so local outbox can purge payload.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

# Optional boto3 clients when running in live AWS Lambda
try:
    import boto3
    _dynamodb = boto3.resource("dynamodb") if os.getenv("AWS_DEFAULT_REGION") else None
    _eventbridge = boto3.client("events") if os.getenv("AWS_DEFAULT_REGION") else None
except Exception:
    _dynamodb = None
    _eventbridge = None

import importlib
try:
    _auth = importlib.import_module("lambda.auth")
except Exception:
    try:
        import auth as _auth
    except Exception:
        _auth = None

CONNECTIONS_TABLE_NAME = os.getenv("AURA_CONNECTIONS_TABLE", "AuraWebSocketConnections")
EVENT_BUS_NAME = os.getenv("AURA_EVENT_BUS", "aura-event-bus")
PROHIBITED_RAW_KEYS = {"raw_audio", "full_transcript", "audio_blob", "unredacted_speech"}


# ---------------------------------------------------------------------------
# Structured logger
# ---------------------------------------------------------------------------

def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a single structured JSON log line to stdout.

    Fields are kept to a minimum necessary set — no raw user content.
    ``correlation_id`` is always included when present in *fields*.
    """
    record = {
        "timestamp": time.time(),
        "level": level,
        "service": "aura.websocket_handler",
        "event": event,
        **fields,
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def validate_connect_token(token: str | None) -> tuple[bool, dict[str, str]]:
    """Cryptographically validate bearer token and extract verified subject claims.

    Verifies OIDC/Cognito JWT signatures against JWKS using RS256.
    Ensures token has not expired and contains valid subject identity.
    """
    if not token or not token.strip():
        _log("WARNING", "token_validation_rejected", reason="empty_token")
        return False, {}
    token_str = token.strip()
    if token_str.lower().startswith("bearer "):
        token_str = token_str[7:].strip()
    if not token_str:
        _log("WARNING", "token_validation_rejected", reason="empty_token")
        return False, {}

    if _auth is not None:
        try:
            claims = _auth.verify_and_decode_bearer_jwt(token_str)
            user_id = str(claims.get("sub") or claims.get("cognito:username") or claims.get("username") or "")
            tenant_id = str(claims.get("custom:tenant_id") or claims.get("tenant_id") or "tenant_default")
            if not user_id:
                _log("WARNING", "token_validation_rejected", reason="missing_subject_claim")
                return False, {}
            _log("INFO", "token_validation_accepted", user_id=user_id, tenant_id=tenant_id)
            return True, {"user_id": user_id, "tenant_id": tenant_id}
        except _auth.AuthError as err:
            _log("WARNING", "token_validation_rejected", reason=err.error, message=err.message)
            return False, {}
        except Exception as exc:
            _log("WARNING", "token_validation_rejected", reason="jwt_verification_failed", error=str(exc))
            return False, {}

    _log("ERROR", "token_validation_unavailable", reason="auth_module_unavailable")
    return False, {}


def handle_connect(event: dict[str, Any], table: Any = None) -> dict[str, Any]:
    connection_id = event.get("requestContext", {}).get("connectionId")
    query_params = event.get("queryStringParameters") or {}
    headers = event.get("headers") or {}
    correlation_id = str(uuid.uuid4())

    if not connection_id:
        _log("WARNING", "connect_rejected", reason="missing_connection_id")
        return {"statusCode": 403, "body": json.dumps({"error": "unauthorized", "message": "Valid connectionId required"})}

    # 1. Prefer trusted API Gateway authorizer claims if present
    authorizer = event.get("requestContext", {}).get("authorizer") or {}
    auth_claims = authorizer.get("jwt", {}).get("claims") or authorizer.get("claims")
    if isinstance(auth_claims, dict) and (auth_claims.get("sub") or auth_claims.get("cognito:username") or auth_claims.get("username")):
        verified_user_id = str(auth_claims.get("sub") or auth_claims.get("cognito:username") or auth_claims.get("username"))
        verified_tenant_id = str(auth_claims.get("custom:tenant_id") or auth_claims.get("tenant_id") or "tenant_default")
    else:
        # 2. Cryptographically verify token from Authorization header or query parameter
        header_token = headers.get("Authorization") or headers.get("authorization")
        qs_token = query_params.get("auth_token") or query_params.get("token")
        token = header_token or qs_token
        if qs_token and not header_token:
            _log("WARNING", "connect_token_from_querystring",
                 connection_id=connection_id,
                 note="token in querystring appears in access logs; prefer Authorization header")

        valid, claims = validate_connect_token(token)
        if not valid:
            _log("WARNING", "connect_rejected",
                 correlation_id=correlation_id,
                 connection_id=connection_id,
                 reason="invalid_token")
            return {"statusCode": 403, "body": json.dumps({"error": "unauthorized", "message": "Valid token required"})}

        verified_user_id = claims.get("user_id")
        verified_tenant_id = claims.get("tenant_id", "tenant_default")

    if not verified_user_id:
        _log("WARNING", "connect_rejected",
             correlation_id=correlation_id,
             connection_id=connection_id,
             reason="missing_verified_user_id")
        return {"statusCode": 403, "body": json.dumps({"error": "unauthorized", "message": "Valid user identity required"})}

    # Untrusted client parameter checks: caller cannot override or mismatch verified identity
    req_user_id = query_params.get("user_id")
    if req_user_id and req_user_id != verified_user_id:
        _log("WARNING", "connect_user_mismatch_rejected",
             correlation_id=correlation_id,
             connection_id=connection_id,
             requested_user=req_user_id,
             token_user=verified_user_id)
        return {"statusCode": 403, "body": json.dumps({"error": "forbidden", "message": "Caller cannot bind connection to different user"})}

    req_tenant_id = query_params.get("tenant_id")
    if req_tenant_id and req_tenant_id != verified_tenant_id:
        _log("WARNING", "connect_tenant_mismatch_rejected",
             correlation_id=correlation_id,
             connection_id=connection_id,
             requested_tenant=req_tenant_id,
             token_tenant=verified_tenant_id)
        return {"statusCode": 403, "body": json.dumps({"error": "forbidden", "message": "Caller cannot bind connection to different tenant"})}

    user_id = verified_user_id
    tenant_id = verified_tenant_id
    now = time.time()
    ttl = int(now + 7200)  # 2 hours connection TTL

    item = {
        "connectionId": connection_id,
        "userId": user_id,
        "tenantId": tenant_id,
        "connectedAt": now,
        "ttl": ttl,
    }

    if table is not None:
        try:
            table.put_item(Item=item)
        except Exception as exc:
            _log("ERROR", "connect_state_write_failed",
                 correlation_id=correlation_id,
                 connection_id=connection_id,
                 error=str(exc))
            return {"statusCode": 500, "body": json.dumps({"error": "state_error", "message": str(exc)})}

    _log("INFO", "connect_accepted",
         correlation_id=correlation_id,
         connection_id=connection_id,
         user_id=user_id,
         tenant_id=tenant_id,
         ttl=ttl)
    return {"statusCode": 200, "body": json.dumps({"connected": True, "connectionId": connection_id})}


def handle_disconnect(event: dict[str, Any], table: Any = None) -> dict[str, Any]:
    connection_id = event.get("requestContext", {}).get("connectionId")
    if connection_id and table is not None:
        try:
            table.delete_item(Key={"connectionId": connection_id})
        except Exception:
            pass
    _log("INFO", "disconnect_complete", connection_id=connection_id)
    return {"statusCode": 200, "body": json.dumps({"disconnected": True})}


def handle_relay(
    body: dict[str, Any],
    bus_client: Any = None,
    bus_name: str = EVENT_BUS_NAME,
    authenticated_user_id: str | None = None,
    connection_id: str | None = None,
) -> dict[str, Any]:
    """Process incoming relay envelope and return acknowledgement for outbox purging.

    Binds the relay event to the authenticated connection user, blocking any
    impersonation attempts where caller-supplied user_id mismatches authenticated identity.
    """
    event_id = body.get("event_id")
    event_type = body.get("event_type")
    req_user_id = body.get("user_id")
    timestamp = body.get("timestamp") or time.time()
    correlation_id = str(uuid.uuid4())

    # FAIL CLOSED: Reject relay requests whenever authenticated identity cannot be established
    if not authenticated_user_id or not str(authenticated_user_id).strip():
        _log("WARNING", "relay_unauthenticated_request",
             correlation_id=correlation_id,
             connection_id=connection_id,
             reason="missing_authenticated_identity")
        return {
            "statusCode": 403,
            "body": {
                "error": "forbidden",
                "message": "Authenticated identity could not be established for relay event",
                "timestamp": time.time(),
            },
        }

    # Impersonation guard: caller cannot claim a different identity than authenticated connection
    if req_user_id and str(req_user_id).strip() != str(authenticated_user_id).strip():
        _log("WARNING", "relay_user_impersonation_blocked",
             correlation_id=correlation_id,
             requested_user_id=req_user_id,
             authenticated_user_id=authenticated_user_id,
             connection_id=connection_id)
        return {
            "statusCode": 403,
            "body": {
                "error": "forbidden",
                "message": f"Caller '{req_user_id}' cannot impersonate authenticated user '{authenticated_user_id}'",
                "timestamp": time.time(),
            },
        }

    user_id = str(authenticated_user_id).strip()

    # Schema validation
    if not event_id or not event_type or not user_id:
        _log("WARNING", "relay_envelope_invalid",
             correlation_id=correlation_id,
             reason="missing_required_fields",
             present_fields=list(body.keys()))
        return {
            "statusCode": 400,
            "body": {
                "error": "invalid_envelope",
                "message": "Missing required fields: event_id, event_type, user_id",
                "timestamp": time.time(),
            },
        }

    # Oversized payload protection: max 32KB
    payload_size = len(json.dumps(body))
    if payload_size > 32768:
        _log("WARNING", "relay_payload_oversized",
             correlation_id=correlation_id,
             event_id=event_id,
             payload_size=payload_size,
             limit=32768)
        return {
            "statusCode": 400,
            "body": {
                "error": "oversized_payload",
                "message": f"Payload size {payload_size} bytes exceeds 32KB boundary",
                "event_id": event_id,
                "timestamp": time.time(),
            },
        }

    # Strict privacy check: zero raw audio / prohibited persistence
    for prohibited in PROHIBITED_RAW_KEYS:
        if prohibited in body:
            _log("ERROR", "relay_privacy_violation",
                 correlation_id=correlation_id,
                 event_id=event_id,
                 prohibited_field=prohibited)
            return {
                "statusCode": 400,
                "body": {
                    "error": "privacy_violation",
                    "message": f"Prohibited raw field detected: '{prohibited}'. Data must be redacted locally.",
                    "event_id": event_id,
                    "timestamp": time.time(),
                },
            }

    # Evidence bounds check: at most 5 items
    evidence = body.get("evidence", [])
    if len(evidence) > 5:
        _log("WARNING", "relay_evidence_overflow",
             correlation_id=correlation_id,
             event_id=event_id,
             evidence_count=len(evidence),
             limit=5)
        return {
            "statusCode": 400,
            "body": {
                "error": "envelope_overflow",
                "message": "Evidence list exceeds 5-item maximum",
                "event_id": event_id,
                "timestamp": time.time(),
            },
        }

    ack_id = f"ack_{uuid.uuid4().hex[:12]}"
    bus_message_id = f"msg_{uuid.uuid4().hex[:12]}"

    # Publish to EventBridge if client is active
    if bus_client is not None:
        try:
            bus_client.put_events(
                Entries=[
                    {
                        "Source": "aura.companion",
                        "DetailType": "FocusSignalDetected" if event_type == "focus_signal" else "BeeEventSanitized",
                        "Detail": json.dumps(
                            {
                                "event_id": event_id,
                                "user_id": user_id,
                                "session_id": body.get("session_id", "session_default"),
                                "timestamp": timestamp,
                                "explicit_signal": body.get("signal_payload", {}).get("explicit_signal", "none"),
                                "consented_signal_score": body.get("signal_payload", {}).get("consented_signal_score", 0.0),
                                "evidence": evidence,
                                "idempotency_key": f"{user_id}:{event_id}",
                                "correlation_id": correlation_id,
                            }
                        ),
                        "EventBusName": bus_name,
                    }
                ]
            )
        except Exception as exc:
            _log("ERROR", "relay_eventbridge_failed",
                 correlation_id=correlation_id,
                 event_id=event_id,
                 error=str(exc))
            return {
                "statusCode": 500,
                "body": {
                    "error": "eventbridge_delivery_failed",
                    "message": str(exc),
                    "event_id": event_id,
                    "timestamp": time.time(),
                },
            }

    _log("INFO", "relay_accepted",
         correlation_id=correlation_id,
         event_id=event_id,
         event_type=event_type,
         ack_id=ack_id,
         bus_message_id=bus_message_id,
         evidence_count=len(evidence))
    return {
        "statusCode": 200,
        "body": {
            "ack_id": ack_id,
            "event_id": event_id,
            "status": "accepted",
            "bus_message_id": bus_message_id,
            "timestamp": time.time(),
        },
    }


def lambda_handler(
    event: dict[str, Any],
    _context: object = None,
    table_override: Any = None,
    bus_override: Any = None,
) -> dict[str, Any]:
    """Entrypoint for WebSocket API Gateway."""
    route_key = event.get("requestContext", {}).get("routeKey")
    table = table_override or (_dynamodb.Table(CONNECTIONS_TABLE_NAME) if _dynamodb else None)
    bus = bus_override or _eventbridge

    _log("DEBUG", "lambda_handler_invoked", route_key=route_key)

    if route_key == "$connect":
        return handle_connect(event, table=table)

    if route_key == "$disconnect":
        return handle_disconnect(event, table=table)

    raw_body = event.get("body", {})
    if isinstance(raw_body, str):
        try:
            raw_body = json.loads(raw_body)
        except json.JSONDecodeError:
            _log("WARNING", "lambda_handler_malformed_json", route_key=route_key)
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "invalid_json", "message": "Malformed JSON in body"}),
            }

    action = raw_body.get("action")
    if route_key == "relay" or action == "relay":
        connection_id = event.get("requestContext", {}).get("connectionId")
        authenticated_user_id = None

        # Check API Gateway authorizer context first
        authorizer = event.get("requestContext", {}).get("authorizer") or {}
        auth_claims = (
            authorizer.get("jwt", {}).get("claims")
            or authorizer.get("claims")
        )
        if isinstance(auth_claims, dict):
            authenticated_user_id = (
                auth_claims.get("sub")
                or auth_claims.get("cognito:username")
                or auth_claims.get("username")
            )

        # If not in authorizer context, resolve authenticated user from connection table
        if not authenticated_user_id:
            if not connection_id:
                _log("WARNING", "relay_missing_connection_id", route_key=route_key)
                return {
                    "statusCode": 403,
                    "body": json.dumps({
                        "error": "forbidden",
                        "message": "Missing connection ID and absent authorizer claims",
                        "timestamp": time.time(),
                    }),
                }

            if table is None:
                _log("ERROR", "relay_connection_storage_unavailable", connection_id=connection_id)
                return {
                    "statusCode": 403,
                    "body": json.dumps({
                        "error": "forbidden",
                        "message": "Connection storage unavailable to verify connection identity",
                        "timestamp": time.time(),
                    }),
                }

            try:
                conn_res = table.get_item(Key={"connectionId": connection_id})
                conn_item = conn_res.get("Item")
                if conn_item and conn_item.get("userId"):
                    authenticated_user_id = conn_item.get("userId")
                else:
                    _log("WARNING", "relay_unauthenticated_connection", connection_id=connection_id)
                    return {
                        "statusCode": 403,
                        "body": json.dumps({
                            "error": "forbidden",
                            "message": "Connection record missing or not authenticated",
                            "timestamp": time.time(),
                        }),
                    }
            except Exception as exc:
                _log("ERROR", "relay_connection_lookup_failed", connection_id=connection_id, error=str(exc))
                return {
                    "statusCode": 403,
                    "body": json.dumps({
                        "error": "forbidden",
                        "message": f"Connection lookup error: {str(exc)}",
                        "timestamp": time.time(),
                    }),
                }

        res = handle_relay(
            raw_body,
            bus_client=bus,
            bus_name=EVENT_BUS_NAME,
            authenticated_user_id=authenticated_user_id,
            connection_id=connection_id,
        )
        return {
            "statusCode": res["statusCode"],
            "body": json.dumps(res["body"]),
        }

    _log("WARNING", "lambda_handler_unknown_route", route_key=route_key, action=action)
    return {
        "statusCode": 400,
        "body": json.dumps(
            {
                "error": "unknown_route",
                "message": f"Unsupported route or action: '{route_key or action}'",
                "timestamp": time.time(),
            }
        ),
    }
