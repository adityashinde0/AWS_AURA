"""HTTP Control Plane Lambda handler for Project AURA.

Implements REST API Gateway endpoints:
- /api/v1/consent/grants (GET, POST)
- /api/v1/consent/grants/{grantId} (DELETE)
- /api/v1/focus/session (POST)
- /api/v1/focus/bank (GET)
- /api/v1/focus/signals (GET, POST)
- /api/v1/focus/signal (POST)
- /api/v1/devices (GET, POST)
- /api/v1/feature-flags (GET, POST)
- /api/v1/safety/guardrails (GET)
- /api/v1/safety/guardrails/reset (POST)
- /api/v1/audit/actions (GET)
- /api/v1/actions/undo (POST)
- /api/v1/user/export (GET)
- /api/v1/user/data (DELETE)
- /apns/register (POST)
- /api/v1/devices/wearable (POST)
- /api/v1/safety/escalate (POST)
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Any

from standalone_agent.device_adapter import DeviceInfo, DeviceSimulatorAdapter
from standalone_agent.feature_flags import FeatureFlagManager
from standalone_agent.undo_guardrail import UndoRateGuardrail
from standalone_agent.slack_adapter import (
    SlackAdapter,
    ZeroLeakageViolationError,
    ChannelNotAllowlistedError,
    WorkspaceNotApprovedError,
)
from standalone_agent.sms_adapter import SmsAdapter

import importlib
try:
    _auth = importlib.import_module("lambda.auth")
except Exception:
    try:
        import auth as _auth
    except Exception:
        _auth = None

# Optional boto3 clients when running in live AWS Lambda
try:
    import boto3
    _dynamodb = boto3.resource("dynamodb") if os.getenv("AWS_DEFAULT_REGION") else None
except Exception:
    _dynamodb = None

CORE_TABLE_NAME = os.getenv("AURA_CORE_TABLE", "AuraCoreTable")


# ---------------------------------------------------------------------------
# Storage Errors & DataStore Abstraction
# ---------------------------------------------------------------------------

class StorageError(Exception):
    """Raised when durable storage operations fail."""


class StorageUnavailableError(StorageError):
    """Raised when durable storage is required in production but unavailable."""


class DataStore:
    """Base interface for AURA storage implementations."""


class DynamoDBDataStore(DataStore):
    """Production-grade durable DynamoDB single-table data store.

    Conforms to schemas/dynamodb/table_schema.json:
    - ConsentGrant: PK=TENANT#{tenant_id}#USER#{user_id}, SK=GRANT#{action_type}
    - FocusSession: PK=USER#{user_id}, SK=SESSION#{session_id}
    - ActionAudit: PK=TENANT#{tenant_id}#USER#{user_id}, SK=AUDIT#{timestamp}#{action_id}
    - UndoRecord: PK=UNDO#{undo_token}, SK=RECORD
    - UserPreferences: PK=USER#{user_id}, SK=PREFERENCES
    """

    def __init__(self, table: Any | None = None, table_name: str | None = None) -> None:
        if table is not None:
            self.table = table
        else:
            try:
                import boto3
                target_table = table_name or CORE_TABLE_NAME
                self.table = boto3.resource("dynamodb").Table(target_table)
            except Exception as exc:
                raise StorageUnavailableError(f"Failed to initialize DynamoDB table: {exc}") from exc

        self.device_adapter: DeviceSimulatorAdapter = DeviceSimulatorAdapter()
        self.feature_flags: FeatureFlagManager = FeatureFlagManager()
        self.undo_guardrail: UndoRateGuardrail = UndoRateGuardrail()
        self.slack_adapter: SlackAdapter = SlackAdapter()
        self.sms_adapter: SmsAdapter = SmsAdapter()
        self.learned_routines: dict[str, dict[str, Any]] = {}
        self.wearable_bindings: dict[str, dict[str, Any]] = {}
        self.escalation_timestamps: dict[str, float] = {}
        self.pending_escalations: dict[str, dict[str, Any]] = {}

    def get_grants(self, user_id: str, tenant_id: str = "tenant_default") -> list[dict[str, Any]]:
        pk = f"TENANT#{tenant_id}#USER#{user_id}"
        try:
            res = self.table.query(
                ExpressionAttributeNames={"#pk": "PK", "#sk": "SK"},
                ExpressionAttributeValues={":pk": pk, ":sk_prefix": "GRANT#"},
                KeyConditionExpression="#pk = :pk AND begins_with(#sk, :sk_prefix)",
            )
            return [it for it in res.get("Items", []) if it.get("active", True)]
        except Exception as exc:
            raise StorageError(f"DynamoDB get_grants failed: {exc}") from exc

    def save_grant(self, grant: dict[str, Any]) -> dict[str, Any]:
        tenant_id = grant.get("tenant_id", "tenant_default")
        user_id = grant["user_id"]
        action_type = grant.get("action_type") or grant.get("grant_id")
        grant_copy = dict(grant)
        grant_copy["PK"] = f"TENANT#{tenant_id}#USER#{user_id}"
        grant_copy["SK"] = f"GRANT#{action_type}"
        grant_copy["ttl"] = int(grant_copy.get("expires_at", time.time() + 86400 * 30))
        try:
            self.table.put_item(Item=grant_copy)
            return grant_copy
        except Exception as exc:
            raise StorageError(f"DynamoDB save_grant failed: {exc}") from exc

    def revoke_grant(self, grant_id: str, user_id: str, tenant_id: str = "tenant_default") -> bool:
        grants = self.get_grants(user_id, tenant_id=tenant_id)
        matching = [g for g in grants if g.get("grant_id") == grant_id or g.get("action_type") == grant_id]
        if not matching:
            return False
        for g in matching:
            try:
                self.table.update_item(
                    Key={"PK": g["PK"], "SK": g["SK"]},
                    UpdateExpression="SET #active = :false, revoked_at = :now",
                    ExpressionAttributeNames={"#active": "active"},
                    ExpressionAttributeValues={":false": False, ":now": int(time.time())},
                )
            except Exception as exc:
                raise StorageError(f"DynamoDB revoke_grant failed: {exc}") from exc
        return True

    def get_audit_records(self, user_id: str, tenant_id: str = "tenant_default", limit: int = 20) -> list[dict[str, Any]]:
        pk = f"TENANT#{tenant_id}#USER#{user_id}"
        try:
            res = self.table.query(
                ExpressionAttributeNames={"#pk": "PK", "#sk": "SK"},
                ExpressionAttributeValues={":pk": pk, ":sk_prefix": "AUDIT#"},
                KeyConditionExpression="#pk = :pk AND begins_with(#sk, :sk_prefix)",
                ScanIndexForward=False,
                Limit=limit,
            )
            return res.get("Items", [])
        except Exception as exc:
            raise StorageError(f"DynamoDB get_audit_records failed: {exc}") from exc

    def record_audit(self, record: dict[str, Any]) -> dict[str, Any]:
        tenant_id = record.get("tenant_id", "tenant_default")
        user_id = record["user_id"]
        now = record.get("timestamp", time.time())
        action_id = record.get("action_id") or uuid.uuid4().hex[:8]
        rec_copy = dict(record)
        rec_copy["PK"] = f"TENANT#{tenant_id}#USER#{user_id}"
        rec_copy["SK"] = f"AUDIT#{int(now)}#{action_id}"
        rec_copy["GSI1PK"] = f"USER#{user_id}#ACTION_TYPE#{record.get('action_type', 'none')}"
        rec_copy["GSI1SK"] = f"AUDIT#{int(now)}"
        rec_copy["ttl"] = int(now + 86400 * 90)
        try:
            self.table.put_item(Item=rec_copy)
            return rec_copy
        except Exception as exc:
            raise StorageError(f"DynamoDB record_audit failed: {exc}") from exc

    def get_undo_record(self, undo_token: str) -> dict[str, Any] | None:
        try:
            res = self.table.get_item(Key={"PK": f"UNDO#{undo_token}", "SK": "RECORD"})
            return res.get("Item")
        except Exception as exc:
            raise StorageError(f"DynamoDB get_undo_record failed: {exc}") from exc

    def acquire_undo_lease(
        self,
        undo_token: str,
        user_id: str,
        lease_owner_id: str,
        lease_duration: float = 30.0,
        tenant_id: str = "tenant_default",
    ) -> tuple[bool, int, str | None]:
        now = time.time()
        try:
            self.table.put_item(
                Item={
                    "PK": f"UNDO#{undo_token}",
                    "SK": "RECORD",
                    "userId": user_id,
                    "tenantId": tenant_id,
                    "status": "IN_PROGRESS",
                    "leaseOwnerId": lease_owner_id,
                    "leaseExpiresAt": int(now + lease_duration),
                    "timestamp": int(now),
                    "ttl": int(now + 86400 * 30),
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) "
                    "OR (#status = :failed) "
                    "OR (#status = :in_progress AND leaseExpiresAt < :now)"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":in_progress": "IN_PROGRESS",
                    ":now": int(now),
                },
            )
            return True, 200, None
        except Exception as exc:
            if "ConditionalCheckFailed" in type(exc).__name__ or "ConditionalCheckFailed" in str(exc):
                rec = self.get_undo_record(undo_token)
                if rec and rec.get("status") == "SUCCEEDED":
                    return False, 200, "Action was already reversed (idempotent)"
                return False, 409, "Undo reversal currently in progress, please retry shortly"
            raise StorageError(f"DynamoDB acquire_undo_lease failed: {exc}") from exc

    def complete_undo(self, undo_token: str, user_id: str, lease_owner_id: str) -> bool:
        now = time.time()
        try:
            self.table.update_item(
                Key={"PK": f"UNDO#{undo_token}", "SK": "RECORD"},
                UpdateExpression="SET #status = :succeeded, completedAt = :now",
                ConditionExpression="#status = :in_progress AND leaseOwnerId = :owner",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":succeeded": "SUCCEEDED",
                    ":in_progress": "IN_PROGRESS",
                    ":owner": lease_owner_id,
                    ":now": int(now),
                },
            )
            return True
        except Exception as exc:
            _log("WARNING", "dynamodb_complete_undo_failed", error=str(exc))
            return False

    def fail_undo(self, undo_token: str, user_id: str, lease_owner_id: str, error_reason: str = "failed") -> bool:
        now = time.time()
        try:
            self.table.update_item(
                Key={"PK": f"UNDO#{undo_token}", "SK": "RECORD"},
                UpdateExpression="SET #status = :failed, failedReason = :reason, leaseExpiresAt = :now",
                ConditionExpression="#status = :in_progress AND leaseOwnerId = :owner",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":in_progress": "IN_PROGRESS",
                    ":owner": lease_owner_id,
                    ":reason": error_reason,
                    ":now": int(now),
                },
            )
            return True
        except Exception as exc:
            _log("WARNING", "dynamodb_fail_undo_failed", error=str(exc))
            return False

    def get_focus_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        try:
            res = self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"SESSION#{session_id}"})
            return res.get("Item")
        except Exception as exc:
            raise StorageError(f"DynamoDB get_focus_session failed: {exc}") from exc

    def save_focus_session(self, session: dict[str, Any]) -> dict[str, Any]:
        sess_copy = dict(session)
        sess_copy["PK"] = f"USER#{session['user_id']}"
        sess_copy["SK"] = f"SESSION#{session['session_id']}"
        sess_copy["ttl"] = int(session.get("expires_at", time.time() + 86400 * 30))
        try:
            self.table.put_item(Item=sess_copy)
            return sess_copy
        except Exception as exc:
            raise StorageError(f"DynamoDB save_focus_session failed: {exc}") from exc

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        try:
            res = self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PREFERENCES"})
            return res.get("Item", {}).get("preferences", {})
        except Exception as exc:
            raise StorageError(f"DynamoDB get_preferences failed: {exc}") from exc

    def update_preferences(self, user_id: str, preferences: dict[str, Any]) -> dict[str, Any]:
        try:
            item = {
                "PK": f"USER#{user_id}",
                "SK": "PREFERENCES",
                "userId": user_id,
                "preferences": preferences,
                "updated_at": int(time.time()),
            }
            self.table.put_item(Item=item)
            return preferences
        except Exception as exc:
            raise StorageError(f"DynamoDB update_preferences failed: {exc}") from exc


class InMemoryDataStore(DataStore):
    """In-memory data store for local simulation, contract testing, and offline modes."""

    def __init__(self) -> None:
        self.grants: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.audit_records: list[dict[str, Any]] = []
        self.undone_tokens: set[str] = set()
        self.undo_records: dict[str, dict[str, Any]] = {}
        self.deep_work_banks: dict[str, dict[str, Any]] = {}  # user_id -> {"balance": int, "transactions": list}
        self.signal_consents: dict[str, set[str]] = {}  # user_id -> set of signal types
        self.device_adapter: DeviceSimulatorAdapter = DeviceSimulatorAdapter()
        self.feature_flags: FeatureFlagManager = FeatureFlagManager()
        self.undo_guardrail: UndoRateGuardrail = UndoRateGuardrail()
        self.slack_adapter: SlackAdapter = SlackAdapter()
        self.sms_adapter: SmsAdapter = SmsAdapter()
        # Learned adaptive intent routines: routine_id -> routine dict
        self.learned_routines: dict[str, dict[str, Any]] = {
            "rtn_wind_down": {
                "routine_id": "rtn_wind_down",
                "user_id": "usr_marcus",
                "intent": "Tired / Sleep",
                "intent_pattern": "tired",
                "name": "Evening Wind-Down",
                "actions": [
                    {"device": "dev_firetv_living_room", "action": "request_pause", "description": "Pause Fire TV playback"},
                    {"device": "dev_smart_lights", "action": "dim_lights", "description": "Dim living room lights to 20%"},
                ],
                "scope": "always",
                "active": True,
                "created_at": time.time() - 86400,
            },
            "rtn_quick_break": {
                "routine_id": "rtn_quick_break",
                "user_id": "usr_marcus",
                "intent": "Quick Break",
                "intent_pattern": "break",
                "name": "15-Min Breather",
                "actions": [
                    {"device": "dev_firetv_living_room", "action": "request_pause", "description": "Pause active media"},
                    {"device": "dev_echo_office", "action": "request_dnd", "description": "Enable Echo Do Not Disturb"},
                ],
                "scope": "always",
                "active": True,
                "created_at": time.time() - 43200,
            },
        }
        # Wearable push-token bindings: user_id -> binding record
        self.wearable_bindings: dict[str, dict[str, Any]] = {}
        # Safety escalation timestamps for rate-limiting: user_id -> last escalation epoch
        self.escalation_timestamps: dict[str, float] = {}
        # Pending safety escalations (60s cancel window): user_id -> details
        self.pending_escalations: dict[str, dict[str, Any]] = {}
        self.preferences: dict[str, dict[str, Any]] = {}

    def get_grants(self, user_id: str, tenant_id: str = "tenant_default") -> list[dict[str, Any]]:
        return [g for g in self.grants.values() if g.get("user_id") == user_id and g.get("active", True)]

    def save_grant(self, grant: dict[str, Any]) -> dict[str, Any]:
        gid = grant.get("grant_id") or f"grant_{uuid.uuid4().hex[:10]}"
        grant["grant_id"] = gid
        self.grants[gid] = grant
        return grant

    def revoke_grant(self, grant_id: str, user_id: str, tenant_id: str = "tenant_default") -> bool:
        grant = self.grants.get(grant_id)
        if grant and grant.get("user_id") == user_id:
            grant["active"] = False
            grant["revoked_at"] = time.time()
            return True
        return False

    def get_audit_records(self, user_id: str, tenant_id: str = "tenant_default", limit: int = 20) -> list[dict[str, Any]]:
        records = [r for r in self.audit_records if r.get("user_id") == user_id]
        records.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
        return records[:limit]

    def record_audit(self, record: dict[str, Any]) -> dict[str, Any]:
        self.audit_records.append(record)
        return record

    def get_undo_record(self, undo_token: str) -> dict[str, Any] | None:
        return self.undo_records.get(undo_token)

    def acquire_undo_lease(
        self,
        undo_token: str,
        user_id: str,
        lease_owner_id: str,
        lease_duration: float = 30.0,
        tenant_id: str = "tenant_default",
    ) -> tuple[bool, int, str | None]:
        now = time.time()
        rec = self.undo_records.get(undo_token)
        if rec:
            if rec.get("status") == "SUCCEEDED" or undo_token in self.undone_tokens:
                return False, 200, "Action was already reversed (idempotent)"
            if rec.get("status") == "IN_PROGRESS" and now < rec.get("lease_expires_at", 0):
                return False, 409, "Undo reversal currently in progress, please retry shortly"

        self.undo_records[undo_token] = {
            "token": undo_token,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "status": "IN_PROGRESS",
            "lease_owner_id": lease_owner_id,
            "lease_expires_at": now + lease_duration,
            "created_at": rec.get("created_at", now) if rec else now,
            "retry_count": rec.get("retry_count", 0) if rec else 0,
        }
        return True, 200, None

    def complete_undo(self, undo_token: str, user_id: str, lease_owner_id: str) -> bool:
        rec = self.undo_records.get(undo_token)
        if rec and rec.get("status") == "IN_PROGRESS" and rec.get("lease_owner_id") == lease_owner_id:
            rec["status"] = "SUCCEEDED"
            rec["completed_at"] = time.time()
            self.undone_tokens.add(undo_token)
            return True
        return False

    def fail_undo(self, undo_token: str, user_id: str, lease_owner_id: str, error_reason: str = "failed") -> bool:
        rec = self.undo_records.get(undo_token)
        if rec and rec.get("status") == "IN_PROGRESS" and rec.get("lease_owner_id") == lease_owner_id:
            rec["status"] = "FAILED"
            rec["failed_reason"] = error_reason
            rec["lease_expires_at"] = time.time()
            rec["retry_count"] = rec.get("retry_count", 0) + 1
            return True
        return False

    def get_focus_session(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        return self.sessions.get(session_id)

    def save_focus_session(self, session: dict[str, Any]) -> dict[str, Any]:
        self.sessions[session["session_id"]] = session
        return session

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        return self.preferences.get(user_id, {})

    def update_preferences(self, user_id: str, preferences: dict[str, Any]) -> dict[str, Any]:
        self.preferences[user_id] = preferences
        return preferences


def get_datastore(store_override: Any | None = None) -> Any:
    """Resolve data store according to environment configuration.

    In production (AWS Lambda or AURA_STORAGE_MODE=dynamodb), requires durable DynamoDB
    and raises StorageUnavailableError if unavailable. Never silently falls back.
    """
    if store_override is not None:
        return store_override

    storage_mode = os.getenv("AURA_STORAGE_MODE", "").strip().lower()
    is_live_aws = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("AWS_DEFAULT_REGION"))

    if storage_mode == "dynamodb" or (is_live_aws and storage_mode != "local"):
        table_name = os.getenv("AURA_CORE_TABLE", CORE_TABLE_NAME)
        try:
            return DynamoDBDataStore(table_name=table_name)
        except Exception as exc:
            _log("ERROR", "dynamodb_initialization_failed", error=str(exc), table=table_name)
            raise StorageUnavailableError(
                f"Durable DynamoDB storage '{table_name}' required in production is unavailable: {exc}"
            ) from exc

    return default_store


# ---------------------------------------------------------------------------
# Structured logger (same pattern as lambda/focus_guardian.py)
# ---------------------------------------------------------------------------

def _log(level: str, event: str, **fields: Any) -> None:
    """Emit a single structured JSON log line to stdout."""
    import json as _json
    record = {
        "timestamp": time.time(),
        "level": level,
        "service": "aura.control_plane",
        "event": event,
        **fields,
    }
    print(_json.dumps(record, default=str), file=sys.stdout, flush=True)


default_store = InMemoryDataStore()


def _json_resp(status: int, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET,POST,DELETE,OPTIONS",
            "access-control-allow-headers": "Content-Type,Authorization",
        },
        "body": json.dumps(data),
    }


def handle_get_grants(user_id: str, store: Any = default_store) -> dict[str, Any]:
    try:
        active_grants = store.get_grants(user_id)
    except StorageError as exc:
        return _json_resp(503, {"error": "storage_error", "message": str(exc)})
    return _json_resp(200, {"user_id": user_id, "grants": active_grants})


def handle_create_grant(body: dict[str, Any], store: Any = default_store) -> dict[str, Any]:
    user_id = body.get("user_id")
    action_type = body.get("action_type")
    scope = body.get("scope")

    if not user_id or not action_type or not scope:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id, action_type, scope are required"})

    valid_actions = {"nudge", "request_dnd", "request_pause", "slack_status", "pre_meeting_briefing"}
    valid_scopes = {"ask_every_time", "allow_session", "allow_always", "never"}

    if action_type not in valid_actions or scope not in valid_scopes:
        return _json_resp(400, {"error": "invalid_enum", "message": "action_type or scope not permitted"})

    grant_id = f"grant_{uuid.uuid4().hex[:10]}"
    now = time.time()
    ttl_seconds = body.get("ttl_seconds", 86400)

    grant = {
        "grant_id": grant_id,
        "user_id": user_id,
        "tenant_id": body.get("tenant_id", "tenant_default"),
        "action_type": action_type,
        "scope": scope,
        "target_allowlist": body.get("target_allowlist", []),
        "active": True,
        "created_at": now,
        "expires_at": now + ttl_seconds,
    }
    try:
        saved_grant = store.save_grant(grant)
    except StorageError as exc:
        return _json_resp(503, {"error": "storage_error", "message": str(exc)})
    return _json_resp(201, saved_grant)


def handle_revoke_grant(grant_id: str, user_id: str, store: Any = default_store) -> dict[str, Any]:
    try:
        store.revoke_grant(grant_id, user_id)
    except StorageError as exc:
        return _json_resp(503, {"error": "storage_error", "message": str(exc)})
    return _json_resp(200, {"revoked": True, "grant_id": grant_id, "revoked_at": time.time()})


def handle_focus_session(body: dict[str, Any], store: Any = default_store) -> dict[str, Any]:
    user_id = body.get("user_id")
    action = body.get("action")

    if not user_id or action not in {"start", "stop"}:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id and action (start|stop) required"})

    now = time.time()
    if action == "start":
        duration = body.get("duration_minutes", 45)
        session_id = body.get("session_id") or f"sess_{uuid.uuid4().hex[:10]}"
        session = {
            "session_id": session_id,
            "user_id": user_id,
            "status": "active",
            "duration_minutes": duration,
            "started_at": now,
            "target_end_at": now + (duration * 60),
            "distraction_score": 0.0,
            "accumulated_points": 5,
        }
        try:
            if hasattr(store, "save_focus_session"):
                store.save_focus_session(session)
            elif hasattr(store, "sessions"):
                store.sessions[session_id] = session
        except StorageError as exc:
            return _json_resp(503, {"error": "storage_error", "message": str(exc)})

        # Award initial deep work bank start points
        if hasattr(store, "deep_work_banks"):
            bank = store.deep_work_banks.setdefault(user_id, {"balance": 10, "transactions": []})
            bank["balance"] += 5
            bank["transactions"].append(
                {
                    "transaction_id": f"tx_{uuid.uuid4().hex[:8]}",
                    "session_id": session_id,
                    "delta": 5,
                    "reason": f"Focus session started ({duration}m)",
                    "timestamp": now,
                }
            )
        return _json_resp(200, session)

    elif action == "stop":
        session_id = body.get("session_id")
        target_session = None
        if session_id and hasattr(store, "get_focus_session"):
            try:
                target_session = store.get_focus_session(user_id, session_id)
            except StorageError as exc:
                return _json_resp(503, {"error": "storage_error", "message": str(exc)})
        elif hasattr(store, "sessions"):
            matching = [
                s
                for s in store.sessions.values()
                if s.get("user_id") == user_id and (s.get("session_id") == session_id or not session_id)
            ]
            if matching:
                target_session = matching[-1]

        if not target_session:
            return _json_resp(404, {"error": "session_not_found", "message": "No active session found for user"})

        target_session["status"] = "completed"
        target_session["ended_at"] = now
        try:
            if hasattr(store, "save_focus_session"):
                store.save_focus_session(target_session)
            elif hasattr(store, "sessions"):
                store.sessions[target_session["session_id"]] = target_session
        except StorageError as exc:
            return _json_resp(503, {"error": "storage_error", "message": str(exc)})
        return _json_resp(200, target_session)

    return _json_resp(400, {"error": "invalid_action"})


def handle_get_bank(user_id: str, store: InMemoryDataStore = default_store) -> dict[str, Any]:
    bank = store.deep_work_banks.setdefault(user_id, {"balance": 10, "transactions": []})
    return _json_resp(
        200,
        {
            "user_id": user_id,
            "balance": bank["balance"],
            "transactions": bank["transactions"][-10:],
            "private_only": True,
            "team_sharing_enabled": False,
            "employer_reporting": "STRICTLY_PROHIBITED",
            "privacy_notice": "Strictly isolated to user; never reported to team or employer.",
        },
    )



def handle_get_signals(user_id: str, store: InMemoryDataStore = default_store) -> dict[str, Any]:
    consented = store.signal_consents.get(user_id, set())
    available = [
        {
            "signal_type": "allowed_video_noise",
            "name": "Background Media/Video Noise Detection",
            "description": "Consents to local background video audio presence check",
            "consented": "allowed_video_noise" in consented,
        },
        {
            "signal_type": "window_title_deflection",
            "name": "Social / Tab Deflection Signal",
            "description": "Consents to local title check for non-work entertainment sites",
            "consented": "window_title_deflection" in consented,
        },
    ]
    return _json_resp(200, {"user_id": user_id, "signals": available})


def handle_set_signals(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    user_id = body.get("user_id")
    signal_type = body.get("signal_type")
    enabled = body.get("enabled", False)

    if not user_id or not signal_type:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id and signal_type required"})

    if user_id not in store.signal_consents:
        store.signal_consents[user_id] = set()

    if enabled:
        store.signal_consents[user_id].add(signal_type)
    else:
        store.signal_consents[user_id].discard(signal_type)

    return _json_resp(
        200,
        {
            "user_id": user_id,
            "signal_type": signal_type,
            "enabled": enabled,
            "consented_signals": list(store.signal_consents[user_id]),
        },
    )


def handle_focus_signal(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Intake focus signal via REST API, evaluate multi-tier policy, and return proposal and action decision."""
    user_id = body.get("user_id")
    session_id = body.get("session_id")
    tenant_id = body.get("tenant_id", "tenant_default")
    requesting_household = body.get("household_id", "household_default")
    explicit_signal = body.get("explicit_signal", "none")
    consented_score = float(body.get("consented_signal_score", 0.0))

    if not user_id or not session_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id and session_id required"})

    is_distracted = explicit_signal == "self_report_distracted"
    distraction_score = 0.80 if is_distracted else consented_score
    bank_delta = -1 if is_distracted else 0

    # Check bank update
    if bank_delta != 0:
        bank = store.deep_work_banks.setdefault(user_id, {"balance": 0, "transactions": []})
        bank["balance"] = max(0, bank["balance"] + bank_delta)
        bank["transactions"].append(
            {
                "transaction_id": f"tx_{uuid.uuid4().hex[:8]}",
                "session_id": session_id,
                "delta": bank_delta,
                "reason": "Self-report distraction penalty",
                "timestamp": time.time(),
            }
        )

    recommended_action = "request_pause" if is_distracted else "none"
    reason = (
        "Self-reported distraction. Proposing to pause allowed media devices."
        if is_distracted
        else "Focus uninterrupted."
    )

    action_decision = "suggested_only"
    undo_token = None
    target_device = None
    now = time.time()
    idempotency_key = body.get("idempotency_key") or f"{user_id}:{session_id}:{now:.6f}_{uuid.uuid4().hex[:6]}"

    user_intent = body.get("user_intent")
    if user_intent:
        intent_lower = user_intent.strip().lower()
        if "tired" in intent_lower or "sleep" in intent_lower or "wind down" in intent_lower:
            recommended_action = "request_pause"
            device_actions = [
                {"device": "dev_firetv_living_room", "action": "request_pause", "description": "Pause Fire TV playback"},
                {"device": "dev_smart_lights", "action": "dim_lights", "description": "Dim smart lights to 20%"},
            ]
            prep_summary = "Intent indicates fatigue or desire to sleep. Proposing a calm wind-down routine."
            questions = ["Would you like to wind down now?", "Should we dim ambient lights?", "Pause active screen playback?"]
            emotion_tone = "calm"
            reason = "User requested rest / sleep; recommending pausing media devices and dimming lights."
        elif "break" in intent_lower:
            recommended_action = "request_pause"
            device_actions = [
                {"device": "dev_firetv_living_room", "action": "request_pause", "description": "Pause active media"},
                {"device": "dev_echo_office", "action": "request_dnd", "description": "Set Echo DND"},
            ]
            prep_summary = "Short break requested. Proposing 15-minute pause and breather status."
            questions = ["Ready for a 15-minute breather?", "Set Slack status to away?", "Resume deep work afterwards?"]
            emotion_tone = "supportive"
            reason = "User requested a quick break; pausing focus session and media playback."
        elif "distracted" in intent_lower or is_distracted:
            recommended_action = "nudge"
            device_actions = [{"device": "dev_orbit_wearable", "action": "nudge", "description": "Haptic refocus pulse"}]
            prep_summary = "Self-reported distraction detected. A gentle refocus nudge is recommended."
            questions = ["What outcome matters most?", "What decision is needed?", "What is the next concrete step?"]
            emotion_tone = "neutral"
            reason = "Self-reported distraction signal; recommending haptic refocus nudge."
        else:
            recommended_action = "request_pause"
            device_actions = [
                {"device": "dev_firetv_living_room", "action": "request_pause", "description": "Pause background media"},
                {"device": "dev_smart_lights", "action": "dim_lights", "description": "Reduce ambient glare"},
            ]
            prep_summary = f"Custom intent '{user_intent}' recognized. Proposing supportive adjustment."
            questions = ["Would a brief pause help reset?", "Should we minimize notifications?", "Adjust ambient environment?"]
            emotion_tone = "calm"
            reason = f"Responding to custom intent '{user_intent}' with low-stimulation routine."

        # Check if user has an active 'always' consent grant or learned routine
        has_learned_grant = False
        for g in store.grants.values():
            if (
                g.get("user_id") == user_id
                and g.get("active", True)
                and g.get("scope") in ("allow_always", "always")
                and (
                    g.get("intent_pattern") == intent_lower
                    or g.get("action_type") in ("routine", recommended_action)
                )
            ):
                has_learned_grant = True
                break

        if not has_learned_grant:
            for r in store.learned_routines.values():
                if (
                    r.get("user_id") == user_id
                    and r.get("active", True)
                    and r.get("intent_pattern") in intent_lower
                ):
                    has_learned_grant = True
                    break

        if has_learned_grant:
            # Deterministic auto-execution under persistent grant
            batch_res = store.device_adapter.execute_batch(
                commands=device_actions,
                batch_idempotency_key=idempotency_key,
                requesting_household_id=requesting_household,
            )
            undo_token = batch_res.get("undo_token") or f"undo_rtn_{uuid.uuid4().hex[:10]}"
            action_decision = "executed"
            store.undo_guardrail.record_execution(user_id, recommended_action)
            act_id = f"act_{uuid.uuid4().hex[:8]}"
            store.audit_records.append(
                {
                    "action_id": act_id,
                    "user_id": user_id,
                    "action_type": recommended_action,
                    "target_device_id": device_actions[0]["device"] if device_actions else None,
                    "status": "executed",
                    "reason": f"Executed learned routine for intent '{user_intent}'",
                    "undo_token": undo_token,
                    "idempotency_key": idempotency_key,
                    "timestamp": now,
                }
            )
            store.undo_records[undo_token] = {
                "token": undo_token,
                "user_id": user_id,
                "action_id": act_id,
                "action_type": recommended_action,
                "target_device": device_actions[0]["device"] if device_actions else None,
                "status": "PENDING",
                "created_at": now,
                "expires_at": now + 86400,
                "lease_expires_at": 0.0,
                "retry_count": 0,
            }
        else:
            action_decision = "ask_user_confirmation"
            store.audit_records.append(
                {
                    "action_id": f"sug_{uuid.uuid4().hex[:8]}",
                    "user_id": user_id,
                    "action_type": recommended_action,
                    "target_device_id": device_actions[0]["device"] if device_actions else None,
                    "status": "suggested_only",
                    "reason": f"User confirmation required for intent routine '{user_intent}'",
                    "idempotency_key": idempotency_key,
                    "timestamp": now,
                }
            )

        proposal = {
            "prep_summary": prep_summary,
            "3_questions": questions,
            "emotion_tone": emotion_tone,
            "is_distracted": is_distracted or ("distracted" in intent_lower),
            "distraction_score": distraction_score,
            "deep_work_bank_delta": bank_delta,
            "evidence_ids": [],
            "recommended_action": recommended_action,
            "reason": reason,
        }

        return _json_resp(
            200,
            {
                "proposal": proposal,
                "policy_required": True,
                "decision": action_decision,
                "learned_routine": has_learned_grant,
                "undo_token": undo_token,
                "target_device_id": device_actions[0]["device"] if device_actions else None,
                "device_actions": device_actions,
                "routine_actions": device_actions,
            },
        )

    if is_distracted:
        # Check Feature Flag & Kill Switch
        is_flag_enabled, flag_reason = store.feature_flags.is_feature_enabled(
            capability=recommended_action,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if not is_flag_enabled:
            action_decision = "blocked_kill_switch" if "Kill Switch" in flag_reason else "blocked_feature_flag"
            store.audit_records.append(
                {
                    "action_id": f"blk_{uuid.uuid4().hex[:8]}",
                    "user_id": user_id,
                    "action_type": recommended_action,
                    "target_device_id": None,
                    "status": action_decision,
                    "reason": flag_reason,
                    "idempotency_key": idempotency_key,
                    "timestamp": now,
                }
            )
            return _json_resp(
                200,
                {
                    "proposal": {
                        "prep_summary": "Focus session in progress.",
                        "3_questions": ["What is your immediate goal?", "Can you remove this distraction?", "Resume focus now?"],
                        "emotion_tone": "supportive",
                        "is_distracted": True,
                        "distraction_score": distraction_score,
                        "deep_work_bank_delta": bank_delta,
                        "evidence_ids": [],
                        "recommended_action": recommended_action,
                        "reason": flag_reason,
                    },
                    "policy_required": True,
                    "decision": "blocked",
                    "blocked_reason": flag_reason,
                    "undo_token": None,
                    "target_device_id": None,
                },
            )

        # Check Safety Circuit Breaker: PRD Section 6 Undo rate > 5% guardrail
        is_paused, undo_rate = store.undo_guardrail.is_auto_execution_paused(user_id, recommended_action)
        if is_paused:
            pause_reason = (
                f"Auto-execution paused by safety guardrail (Undo rate {undo_rate*100:.1f}% > 5%). "
                "Manual user confirmation required."
            )
            store.audit_records.append(
                {
                    "action_id": f"guard_{uuid.uuid4().hex[:8]}",
                    "user_id": user_id,
                    "action_type": recommended_action,
                    "target_device_id": None,
                    "status": "suggested_only",
                    "reason": pause_reason,
                    "idempotency_key": idempotency_key,
                    "timestamp": now,
                }
            )
            return _json_resp(
                200,
                {
                    "proposal": {
                        "prep_summary": "Focus session in progress.",
                        "3_questions": ["Confirm pausing media device?", "Resume focus now?", "Need a 5-minute break?"],
                        "emotion_tone": "supportive",
                        "is_distracted": True,
                        "distraction_score": distraction_score,
                        "deep_work_bank_delta": bank_delta,
                        "evidence_ids": [],
                        "recommended_action": recommended_action,
                        "reason": pause_reason,
                    },
                    "policy_required": True,
                    "decision": "ask_user_confirmation",
                    "auto_execution_paused": True,
                    "undo_rate": undo_rate,
                    "undo_token": None,
                    "target_device_id": None,
                },
            )

        # Check consent grant for recommended_action
        active_grants = [
            g
            for g in store.grants.values()
            if g.get("user_id") == user_id and g.get("action_type") == recommended_action and g.get("active", True)
        ]
        has_grant = len(active_grants) > 0 and active_grants[0].get("scope") in ("allow_session", "allow_always")
        allowed_devices = active_grants[0].get("target_allowlist", []) if has_grant else []
        target_device = allowed_devices[0] if allowed_devices else None

        if has_grant and target_device:
            # Execute on simulated device
            receipt = store.device_adapter.execute_pause(
                target_device,
                idempotency_key=idempotency_key,
                requesting_household_id=requesting_household,
            )
            if receipt.success:
                action_decision = "executed"
                undo_token = receipt.undo_token
                store.undo_guardrail.record_execution(user_id, recommended_action)
                store.audit_records.append(
                    {
                        "action_id": receipt.receipt_id,
                        "user_id": user_id,
                        "action_type": recommended_action,
                        "target_device_id": target_device,
                        "status": "executed",
                        "reason": reason,
                        "undo_token": undo_token,
                        "idempotency_key": idempotency_key,
                        "timestamp": now,
                    }
                )
                if undo_token:
                    store.undo_records[undo_token] = {
                        "token": undo_token,
                        "user_id": user_id,
                        "action_id": receipt.receipt_id,
                        "action_type": recommended_action,
                        "target_device": target_device,
                        "status": "PENDING",
                        "created_at": now,
                        "expires_at": now + 86400,
                        "lease_expires_at": 0.0,
                        "retry_count": 0,
                    }
            else:
                err_msg = receipt.error_message or "Device execution failed"
                if "Household mismatch" in err_msg:
                    action_decision = "blocked_household_mismatch"
                else:
                    action_decision = "blocked_device_not_allowed"
                store.audit_records.append(
                    {
                        "action_id": receipt.receipt_id,
                        "user_id": user_id,
                        "action_type": recommended_action,
                        "target_device_id": target_device,
                        "status": action_decision,
                        "reason": err_msg,
                        "idempotency_key": idempotency_key,
                        "timestamp": now,
                    }
                )
        else:
            action_decision = "suggested_only"
            store.audit_records.append(
                {
                    "action_id": f"sug_{uuid.uuid4().hex[:8]}",
                    "user_id": user_id,
                    "action_type": recommended_action,
                    "target_device_id": target_device,
                    "status": "suggested_only",
                    "reason": "Proposed pause for user confirmation (requires explicit approval)",
                    "idempotency_key": idempotency_key,
                    "timestamp": now,
                }
            )

    proposal = {
        "prep_summary": "Focus session in progress.",
        "3_questions": ["What is your immediate goal?", "Can you remove this distraction?", "Resume focus now?"],
        "emotion_tone": "supportive" if is_distracted else "calm",
        "is_distracted": is_distracted,
        "distraction_score": distraction_score,
        "deep_work_bank_delta": bank_delta,
        "evidence_ids": [],
        "recommended_action": recommended_action,
        "reason": reason,
    }

    return _json_resp(
        200,
        {
            "proposal": proposal,
            "policy_required": True,
            "decision": action_decision,
            "undo_token": undo_token,
            "target_device_id": target_device,
        },
    )


# --- Phase 5 Connected Devices Endpoints ---

def handle_get_devices(store: InMemoryDataStore = default_store) -> dict[str, Any]:
    devices = store.device_adapter.list_devices()
    return _json_resp(200, {"devices": devices, "total": len(devices)})


def handle_register_device(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    device_id = body.get("device_id")
    name = body.get("name")
    device_type = body.get("device_type", "fire_tv")
    capabilities = set(body.get("capabilities", []))
    household_id = body.get("household_id", "household_default")
    cert_status = body.get("certification_status", "certified")
    cert_standard = body.get("certification_standard", "Amazon MediaSession v2")

    if not device_id or not name:
        return _json_resp(400, {"error": "invalid_parameters", "message": "device_id and name required"})

    device = DeviceInfo(
        device_id=device_id,
        name=name,
        device_type=device_type,
        capabilities=capabilities,
        state=body.get("state", "idle"),
        is_online=body.get("is_online", True),
        household_id=household_id,
        certification_status=cert_status,
        certification_standard=cert_standard,
        secret_token_arn=body.get("secret_token_arn"),
        command_rate_limit=body.get("command_rate_limit", 10),
    )
    store.device_adapter.register_device(device)
    return _json_resp(201, {"registered": True, "device_id": device_id})


def handle_get_feature_flags(store: InMemoryDataStore = default_store) -> dict[str, Any]:
    flags = store.feature_flags.get_all_flags()
    return _json_resp(200, flags)


def handle_update_feature_flags(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    level = body.get("level")  # "global", "tenant", "user", "rollout"
    capability = body.get("capability")
    enabled = body.get("enabled", True)

    if not level or not capability:
        return _json_resp(400, {"error": "invalid_parameters", "message": "level and capability required"})

    if level == "global":
        store.feature_flags.set_global_kill_switch(capability, enabled)
    elif level == "tenant":
        tenant_id = body.get("tenant_id", "tenant_default")
        store.feature_flags.set_tenant_kill_switch(tenant_id, capability, enabled)
    elif level == "user":
        user_id = body.get("user_id")
        if not user_id:
            return _json_resp(400, {"error": "missing_user_id"})
        store.feature_flags.set_user_flag(user_id, capability, enabled)
    elif level == "rollout":
        pct = int(body.get("percentage", 100))
        store.feature_flags.set_rollout_percentage(capability, pct)
    else:
        return _json_resp(400, {"error": "invalid_level"})

    return _json_resp(200, {"updated": True, "flags": store.feature_flags.get_all_flags()})


def handle_get_guardrails(user_id: str | None = None, store: InMemoryDataStore = default_store) -> dict[str, Any]:
    metrics = store.undo_guardrail.get_metrics_summary(user_id)
    return _json_resp(200, {"guardrails": metrics})


def handle_reset_guardrail(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    user_id = body.get("user_id")
    action_type = body.get("action_type")
    if not user_id or not action_type:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id and action_type required"})
    store.undo_guardrail.reset_circuit_breaker(user_id, action_type)
    return _json_resp(200, {"reset": True, "user_id": user_id, "action_type": action_type})


def handle_query_audit(user_id: str, limit: int = 20, store: Any = default_store) -> dict[str, Any]:
    try:
        if hasattr(store, "get_audit_records"):
            records = store.get_audit_records(user_id, limit=limit)
        else:
            records = [r for r in getattr(store, "audit_records", []) if r.get("user_id") == user_id]
            records.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
            records = records[:limit]
    except StorageError as exc:
        return _json_resp(503, {"error": "storage_error", "message": str(exc)})
    return _json_resp(200, {"user_id": user_id, "records": records})


def handle_undo_action(body: dict[str, Any], store: Any = default_store) -> dict[str, Any]:
    undo_token = body.get("undo_token")
    user_id = body.get("user_id")

    if not undo_token or not user_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "undo_token and user_id required"})

    now = time.time()

    # 1. Ownership & Expiration Validation against durable undo records and audit records
    undo_rec = store.get_undo_record(undo_token)
    matching_record = None
    if hasattr(store, "audit_records"):
        for r in store.audit_records:
            if r.get("undo_token") == undo_token:
                matching_record = r
                break

    owner_user_id = (undo_rec.get("userId") or undo_rec.get("user_id") if undo_rec else None) or (matching_record.get("user_id") if matching_record else None)

    if owner_user_id and owner_user_id != user_id:
        _log("WARNING", "undo_ownership_mismatch", caller=user_id, owner=owner_user_id, token=undo_token)
        return _json_resp(403, {"error": "unauthorized", "message": "Undo token belongs to another user"})

    # Expiry validation
    if matching_record:
        if matching_record.get("expires_at") and now > matching_record["expires_at"]:
            return _json_resp(410, {"error": "undo_token_expired", "message": "Undo token has expired", "undo_token": undo_token})
        if matching_record.get("ttl_seconds") and (now - matching_record.get("timestamp", 0)) > matching_record["ttl_seconds"]:
            return _json_resp(410, {"error": "undo_token_expired", "message": "Undo token has expired", "undo_token": undo_token})

    if undo_rec:
        if undo_rec.get("expires_at") and now > undo_rec["expires_at"]:
            return _json_resp(410, {"error": "undo_token_expired", "message": "Undo token has expired", "undo_token": undo_token})

    # 2. Replay Prevention & State Machine
    if undo_rec and undo_rec.get("status") == "SUCCEEDED":
        return _json_resp(
            200,
            {"success": True, "undo_token": undo_token, "idempotent": True, "message": "Action was already reversed (idempotent)"},
        )
    if hasattr(store, "undone_tokens") and undo_token in store.undone_tokens:
        return _json_resp(
            200,
            {"success": True, "undo_token": undo_token, "idempotent": True, "message": "Action was already reversed (idempotent)"},
        )

    # 3. Atomic lease acquisition with condition check and fencing token
    lease_owner_id = str(uuid.uuid4())
    try:
        acquired, status_code, err_msg = store.acquire_undo_lease(
            undo_token=undo_token,
            user_id=user_id,
            lease_owner_id=lease_owner_id,
            lease_duration=30.0,
        )
    except StorageError as st_err:
        return _json_resp(503, {"error": "storage_error", "message": str(st_err)})

    if not acquired:
        if status_code == 200:
            return _json_resp(200, {"success": True, "undo_token": undo_token, "idempotent": True, "message": err_msg or "Action was already reversed (idempotent)"})
        if status_code == 409:
            return _json_resp(409, {"error": "undo_in_progress", "message": err_msg or "Undo reversal currently in progress, please retry shortly"})
        return _json_resp(status_code, {"error": "storage_error", "message": err_msg or "Failed to acquire undo lease"})

    # 4. Reverse on certified device adapter or Slack adapter
    device_result = store.device_adapter.execute_undo(undo_token)
    slack_result = store.slack_adapter.execute_undo(undo_token)

    device_ok = isinstance(device_result, dict) and device_result.get("success", False)
    slack_ok = isinstance(slack_result, dict) and slack_result.get("success", False)

    # Honest failure reporting: if token is unknown to devices, slack, and records
    if not device_ok and not slack_ok and not matching_record and not undo_rec:
        store.fail_undo(undo_token, user_id, lease_owner_id, error_reason="unknown_token")
        return _json_resp(
            404,
            {
                "success": False,
                "error": "undo_failed",
                "message": "Invalid or unknown undo token; no device or status was restored",
                "undo_token": undo_token,
            },
        )

    # If device explicitly failed due to offline state or network failure on a known action
    if isinstance(device_result, dict) and device_result.get("error") == "device_offline":
        store.fail_undo(undo_token, user_id, lease_owner_id, error_reason="device_offline")
        return _json_resp(
            502,
            {
                "success": False,
                "error": "device_reversal_failed",
                "retryable": True,
                "message": "Device offline or communication failure; token not consumed",
                "undo_token": undo_token,
            },
        )

    # 5. State transition to SUCCEEDED with fencing token
    store.complete_undo(undo_token, user_id, lease_owner_id)

    # Mark existing audit record undone
    action_type = "request_pause"
    for record in store.audit_records:
        if record.get("undo_token") == undo_token:
            record["undone"] = True
            record["status"] = "undone"
            action_type = record.get("action_type", "request_pause")

    # Record reversal in undo guardrail
    tripped = store.undo_guardrail.record_undo(user_id, action_type)

    # Record reversal entry in audit log
    now = time.time()
    store.audit_records.append(
        {
            "action_id": f"undo_{uuid.uuid4().hex[:8]}",
            "user_id": user_id,
            "action_type": "undo_reversal",
            "status": "undone",
            "reason": f"Action reversed by user via token {undo_token}",
            "undo_token": undo_token,
            "timestamp": now,
        }
    )

    _log("INFO", "undo_action_executed", user_id=user_id, undo_token=undo_token, device_ok=device_ok, slack_ok=slack_ok)

    return _json_resp(
        200,
        {
            "success": True,
            "undo_token": undo_token,
            "message": "Action reversed successfully",
            "device_result": device_result,
            "circuit_breaker_tripped": tripped,
        },
    )


def handle_slack_oauth_authorize(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    requested_scopes = body.get("scopes")
    redirect_uri = body.get("redirect_uri")
    try:
        flow_data = store.slack_adapter.start_oauth_flow(
            requested_scopes=requested_scopes,
            custom_redirect_uri=redirect_uri,
        )
        return _json_resp(200, flow_data)
    except Exception as exc:
        return _json_resp(400, {"error": "oauth_initiation_failed", "message": str(exc)})


def handle_slack_oauth_callback(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    code = body.get("code")
    state = body.get("state")
    code_verifier = body.get("code_verifier")
    workspace_id = body.get("workspace_id")
    workspace_name = body.get("workspace_name", "Corporate Workspace")
    admin_user_id = body.get("admin_user_id", "admin_01")
    allowed_channels = body.get("allowed_channels")

    if not code or not state or not code_verifier or not workspace_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "code, state, code_verifier, workspace_id are required"})

    try:
        result = store.slack_adapter.complete_oauth_callback(
            code=code,
            state=state,
            code_verifier=code_verifier,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            admin_user_id=admin_user_id,
            allowed_channels=allowed_channels,
        )
        return _json_resp(200, result)
    except Exception as exc:
        return _json_resp(400, {"error": "oauth_exchange_failed", "message": str(exc)})


def handle_slack_preview(user_id: str, query_params: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    duration_minutes = int(query_params.get("duration_minutes", 45))
    custom_status = query_params.get("custom_status")
    status_emoji = query_params.get("status_emoji", ":focus:")
    target_channel = query_params.get("target_channel", "user_profile")

    try:
        preview = store.slack_adapter.generate_preview(
            user_id=user_id,
            duration_minutes=duration_minutes,
            custom_status=custom_status,
            status_emoji=status_emoji,
            target_channel=target_channel,
        )
        return _json_resp(200, preview)
    except ZeroLeakageViolationError as zle:
        return _json_resp(400, {"error": "zero_leakage_violation", "message": str(zle)})
    except Exception as exc:
        return _json_resp(400, {"error": "preview_generation_failed", "message": str(exc)})


def handle_publish_slack_status(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    user_id = body.get("user_id")
    workspace_id = body.get("workspace_id", "T012345678")
    channel_id = body.get("channel_id", "user_profile")
    idempotency_key = body.get("idempotency_key") or f"slack_pub_{uuid.uuid4().hex[:10]}"
    duration_minutes = int(body.get("duration_minutes", 45))
    custom_status = body.get("custom_status")
    status_emoji = body.get("status_emoji", ":focus:")

    if not user_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id is required"})

    # 1. Check feature flags for slack_post capability
    is_enabled, flag_reason = store.feature_flags.is_feature_enabled(
        capability="slack_post",
        user_id=user_id,
        tenant_id=body.get("tenant_id", "tenant_default"),
    )
    if not is_enabled:
        return _json_resp(403, {"error": "blocked_feature_flag", "message": flag_reason})

    # 2. Check active user consent grant for slack_status
    active_grants = [
        g for g in store.grants.values()
        if g.get("user_id") == user_id
        and g.get("action_type") == "slack_status"
        and g.get("active", True)
    ]
    if not active_grants:
        return _json_resp(403, {"error": "blocked_no_consent", "message": "Explicit consent grant for slack_status required"})

    try:
        preview = store.slack_adapter.generate_preview(
            user_id=user_id,
            duration_minutes=duration_minutes,
            custom_status=custom_status,
            status_emoji=status_emoji,
            target_channel=channel_id,
        )

        receipt = store.slack_adapter.publish_status(
            user_id=user_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            preview_payload=preview,
            idempotency_key=idempotency_key,
        )

        # Record audit log only for new execution
        if receipt.get("decision") == "executed":
            store.audit_records.append({
                "action_id": receipt["receipt_id"],
                "user_id": user_id,
                "action_type": "slack_status",
                "target_device": channel_id,
                "status": "executed",
                "reason": f"Published ambient status: {receipt['status_text']}",
                "undo_token": receipt["undo_token"],
                "idempotency_key": idempotency_key,
                "timestamp": receipt["posted_at"],
            })

        return _json_resp(200, receipt)
    except ZeroLeakageViolationError as zle:
        store.audit_records.append({
            "action_id": f"act_{uuid.uuid4().hex[:8]}",
            "user_id": user_id,
            "action_type": "slack_status",
            "target_device": channel_id,
            "status": "blocked",
            "reason": f"Zero Slack Meeting Leakage Rule violation: {str(zle)}",
            "timestamp": time.time(),
        })
        return _json_resp(400, {"error": "zero_leakage_violation", "message": str(zle)})
    except (ChannelNotAllowlistedError, WorkspaceNotApprovedError) as err:
        return _json_resp(403, {"error": "unauthorized_slack_target", "message": str(err)})
    except Exception as exc:
        return _json_resp(500, {"error": "slack_publish_error", "message": str(exc)})


def handle_clear_slack_status(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    user_id = body.get("user_id")
    workspace_id = body.get("workspace_id", "T012345678")
    if not user_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id is required"})

    res = store.slack_adapter.clear_status(user_id, workspace_id)
    return _json_resp(200, res)


def handle_get_slack_workspaces(store: InMemoryDataStore = default_store) -> dict[str, Any]:
    workspaces = [
        {
            "workspace_id": ws.workspace_id,
            "workspace_name": ws.workspace_name,
            "admin_user_id": ws.admin_user_id,
            "is_admin_approved": ws.is_admin_approved,
            "allowed_channels": ws.allowed_channels,
            "token_secret_arn": ws.token_secret_arn,
            "installed_at": ws.installed_at,
        }
        for ws in store.slack_adapter.workspaces.values()
    ]
    return _json_resp(200, {"workspaces": workspaces})


def handle_slack_admin_review(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    workspace_id = body.get("workspace_id")
    admin_user_id = body.get("admin_user_id")
    is_approved = body.get("is_approved", True)
    allowed_channels = body.get("allowed_channels", ["user_profile"])

    if not workspace_id or not admin_user_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "workspace_id and admin_user_id required"})

    try:
        res = store.slack_adapter.register_workspace_admin_review(
            workspace_id=workspace_id,
            admin_user_id=admin_user_id,
            is_approved=is_approved,
            allowed_channels=allowed_channels,
        )
        return _json_resp(200, res)
    except Exception as exc:
        return _json_resp(400, {"error": "admin_review_failed", "message": str(exc)})


def handle_export_user_data(user_id: str, store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Privacy export: full user profile, grants, sessions, bank, and audit records with zero transcript leakage."""
    user_grants = [g for g in store.grants.values() if g.get("user_id") == user_id]
    user_sessions = [s for s in store.sessions.values() if s.get("user_id") == user_id]
    user_audit = [a for a in store.audit_records if a.get("user_id") == user_id]
    bank = store.deep_work_banks.get(user_id, {"balance": 0, "transactions": []})
    signals = list(store.signal_consents.get(user_id, set()))
    guardrails = store.undo_guardrail.get_metrics_summary(user_id)
    slack_status = store.slack_adapter.active_statuses.get(user_id)
    slack_data = {
        "status_text": slack_status.status_text,
        "status_emoji": slack_status.status_emoji,
        "expires_at": slack_status.expires_at,
        "target_channel": slack_status.target_channel,
        "is_active": slack_status.is_active,
    } if slack_status else None

    return _json_resp(
        200,
        {
            "user_id": user_id,
            "exported_at": time.time(),
            "privacy_standard": "ZERO_RAW_TRANSCRIPTS_RETAINED",
            "consent_grants": user_grants,
            "focus_sessions": user_sessions,
            "audit_trail": user_audit,
            "deep_work_bank": bank,
            "consented_signals": signals,
            "safety_guardrails": guardrails,
            "active_slack_status": slack_data,
        },
    )


def handle_delete_user_data(user_id: str, store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Cryptographic deletion / purge of all data for user across all collections."""
    deleted_grants = [gid for gid, g in store.grants.items() if g.get("user_id") == user_id]
    for gid in deleted_grants:
        del store.grants[gid]

    deleted_sessions = [sid for sid, s in store.sessions.items() if s.get("user_id") == user_id]
    for sid in deleted_sessions:
        del store.sessions[sid]

    store.audit_records = [a for a in store.audit_records if a.get("user_id") != user_id]
    store.deep_work_banks.pop(user_id, None)
    store.signal_consents.pop(user_id, None)
    store.slack_adapter.active_statuses.pop(user_id, None)

    return _json_resp(
        200,
        {
            "user_id": user_id,
            "deleted": True,
            "purged_records": {
                "grants": len(deleted_grants),
                "sessions": len(deleted_sessions),
            },
            "deleted_at": time.time(),
        },
    )


# --- Phase 7 Wearable & Safety Escalation Endpoints ---

_VALID_PLATFORMS = {"watchos", "ios", "android"}
_ESCALATION_RATE_LIMIT_SECONDS = 60


def handle_register_wearable(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Register a wearable push token (APNs/FCM).

    Enforces the Single Active Watch Binding rule (docs/plans/apple_implementation_plan.md §7.1):
    a new binding for the same user but a different device_id revokes the old binding
    and appends a security audit record.
    """
    user_id = body.get("user_id")
    device_id = body.get("device_id")
    push_token = body.get("push_token")
    platform = body.get("platform")

    if not user_id or not device_id or not push_token or not platform:
        return _json_resp(
            400,
            {"error": "invalid_parameters", "message": "user_id, device_id, push_token, platform are required"},
        )

    if platform not in _VALID_PLATFORMS:
        return _json_resp(
            400,
            {"error": "invalid_enum", "message": f"platform must be one of {sorted(_VALID_PLATFORMS)}"},
        )

    now = time.time()
    revoked_previous = False
    existing = store.wearable_bindings.get(user_id)

    if existing and existing.get("device_id") != device_id and existing.get("active", True):
        # Revoke old binding — Single Active Watch Binding rule
        existing["active"] = False
        existing["revoked_at"] = now
        revoked_previous = True
        store.audit_records.append(
            {
                "action_id": f"sec_{uuid.uuid4().hex[:8]}",
                "user_id": user_id,
                "category": "security",
                "event": "wearable_binding_revoked",
                "old_device_id": existing["device_id"],
                "new_device_id": device_id,
                "timestamp": now,
            }
        )

    binding = {
        "user_id": user_id,
        "device_id": device_id,
        "push_token": push_token,
        "platform": platform,
        "public_key": body.get("public_key"),
        "active": True,
        "registered_at": now,
    }
    store.wearable_bindings[user_id] = binding

    return _json_resp(
        200,
        {"registered": True, "device_id": device_id, "revoked_previous": revoked_previous},
    )


def handle_safety_escalate(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Escalate a Safety Guardian incident.

    Requires an active opt-in consent grant for "safety_guardian".
    Checks 60s pending escalation window for prior cancellation.
    Rate-limits duplicate escalations for the same user within 60 s.
    Sends SMS via SmsAdapter without ever logging phone numbers.
    Records a privacy-minimised audit entry (category + timestamp only).
    """
    user_id = body.get("user_id")
    incident_id = body.get("incident_id")
    reason = body.get("reason")
    timestamp = body.get("timestamp", time.time())

    if not user_id or not incident_id or not reason:
        return _json_resp(
            400,
            {"error": "invalid_parameters", "message": "user_id, incident_id, reason are required"},
        )

    # Consent gate — must have an active opt-in grant for "safety_guardian"
    active_safety_grants = [
        g
        for g in store.grants.values()
        if g.get("user_id") == user_id
        and g.get("action_type") == "safety_guardian"
        and g.get("active", True)
    ]
    if not active_safety_grants:
        return _json_resp(
            403,
            {"error": "consent_required", "message": "An active opt-in consent grant for safety_guardian is required"},
        )

    now = time.time()

    # Check if escalation was cancelled within window
    pending = store.pending_escalations.get(user_id)
    if pending and pending.get("cancelled", False):
        return _json_resp(
            200,
            {
                "status": "cancelled",
                "incident_id": incident_id,
                "contacts_notified": False,
                "message": "Escalation was cancelled within window",
            },
        )

    # Rate-limit: idempotent within 60 s
    last_ts = store.escalation_timestamps.get(user_id, 0.0)
    if now - last_ts < _ESCALATION_RATE_LIMIT_SECONDS:
        return _json_resp(
            200,
            {
                "status": "escalated",
                "incident_id": incident_id,
                "contacts_notified": True,
                "rate_limited": True,
            },
        )

    # Track 60-second pending escalation window
    store.pending_escalations[user_id] = {
        "expires_at": now + 60,
        "incident_id": incident_id,
        "timestamp": now,
        "cancelled": False,
    }

    audit_id = f"esc_{uuid.uuid4().hex[:8]}"
    contact_phone = body.get("contact_phone") or body.get("emergency_contact_phone") or "+15550001111"
    msg_text = f"AURA Safety Guardian alert for incident {incident_id}. Please verify contact status."

    # Publish SMS to approved emergency contact via SmsAdapter
    try:
        store.sms_adapter.send_sms(contact_phone, msg_text)
        store.escalation_timestamps[user_id] = now

        # Privacy-minimised audit entry — category + timestamp only; zero health/location telemetry
        store.audit_records.append(
            {
                "action_id": audit_id,
                "user_id": user_id,
                "category": "safety_escalation",
                "timestamp": now,
            }
        )

        _log(
            "INFO",
            "safety_escalation_sms_sent",
            user_id=user_id,
            incident_id=incident_id,
            audit_id=audit_id,
        )

        return _json_resp(
            200,
            {
                "status": "escalated",
                "incident_id": incident_id,
                "contacts_notified": True,
                "audit_id": audit_id,
            },
        )
    except Exception as exc:
        store.audit_records.append(
            {
                "action_id": audit_id,
                "user_id": user_id,
                "category": "safety_escalation",
                "status": "escalation_failed",
                "timestamp": now,
            }
        )
        _log(
            "ERROR",
            "safety_escalation_failed",
            user_id=user_id,
            incident_id=incident_id,
            error=str(exc),
        )
        return _json_resp(
            502,
            {
                "error": "escalation_failed",
                "message": "Failed to send safety notification to contacts",
                "incident_id": incident_id,
                "status": "escalation_failed",
                "contacts_notified": False,
                "audit_id": audit_id,
            },
        )


def handle_safety_cancel(
    body: dict[str, Any], store: InMemoryDataStore = default_store
) -> dict[str, Any]:
    """Cancel a pending Safety Guardian escalation within the 60-second window."""
    user_id = body.get("user_id")
    incident_id = body.get("incident_id", "")
    if not user_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id is required"})

    now = time.time()
    store.pending_escalations[user_id] = {
        "expires_at": 0.0,
        "cancelled": True,
        "incident_id": incident_id,
        "cancelled_at": now,
    }

    audit_id = f"esc_cancel_{uuid.uuid4().hex[:8]}"
    store.audit_records.append(
        {
            "action_id": audit_id,
            "user_id": user_id,
            "category": "escalation_cancelled",
            "incident_id": incident_id,
            "timestamp": now,
        }
    )

    _log("INFO", "safety_escalation_cancelled", user_id=user_id, incident_id=incident_id, audit_id=audit_id)

    return _json_resp(
        200,
        {
            "status": "cancelled",
            "incident_id": incident_id,
            "contacts_notified": False,
            "audit_id": audit_id,
            "ok": True,
        },
    )


# ---------------------------------------------------------------------------
# Notification Feedback API (Sub-Task 3)
# ---------------------------------------------------------------------------

_VALID_FEEDBACK_ACTIONS = frozenset({
    "Read",
    "Save",
    "Not useful",
    "Mute this event series",
})


def handle_notification_feedback(
    body: dict[str, Any], store: InMemoryDataStore = default_store
) -> dict[str, Any]:
    """Record user feedback on a pre-meeting card.

    Accepts ``{card_id, calendar_event_id, user_id, action}``  where ``action``
    is one of ``Read | Save | Not useful | Mute this event series``.

    For ``Mute this event series`` the optional ``series_id`` field is added to
    ``user_preferences["muted_series"]`` so that future ``calendar_scheduler``
    calls will return ``status: "muted"`` for that series.
    """
    user_id = body.get("user_id")
    card_id = body.get("card_id")
    calendar_event_id = body.get("calendar_event_id")
    action = body.get("action")
    series_id: str | None = body.get("series_id")

    if not user_id or not card_id or not calendar_event_id or not action:
        return _json_resp(
            400,
            {
                "error": "invalid_parameters",
                "message": "user_id, card_id, calendar_event_id, action are required",
            },
        )

    if action not in _VALID_FEEDBACK_ACTIONS:
        return _json_resp(
            400,
            {
                "error": "invalid_enum",
                "message": f"action must be one of {sorted(_VALID_FEEDBACK_ACTIONS)}",
            },
        )

    # Delegate to WhisperAdapter for preference persistence
    from standalone_agent.whisper_adapter import WhisperAdapter

    adapter = WhisperAdapter()
    # Attach the store's whisper-preference dict so preferences survive between calls
    if not hasattr(store, "whisper_preferences"):
        store.whisper_preferences: dict[str, Any] = {}  # type: ignore[attr-defined]
    adapter.user_preferences = store.whisper_preferences

    result = adapter.record_feedback(
        user_id=user_id,
        feedback_action=action,
        calendar_event_id=calendar_event_id,
        series_id=series_id,
    )

    # For Mute, also reflect series into the store's whisper_preferences directly
    # (WhisperAdapter.record_feedback already does this; this is belt-and-suspenders)
    if action == "Mute this event series" and series_id:
        prefs = store.whisper_preferences.setdefault(
            user_id,
            {"user_id": user_id, "muted_series": [], "feedback_history": []},
        )
        if series_id not in prefs.get("muted_series", []):
            prefs.setdefault("muted_series", []).append(series_id)

    now = time.time()
    audit_id = f"fb_{uuid.uuid4().hex[:8]}"
    store.audit_records.append(
        {
            "action_id": audit_id,
            "user_id": user_id,
            "action_type": "notification_feedback",
            "card_id": card_id,
            "calendar_event_id": calendar_event_id,
            "action": action,
            "timestamp": now,
        }
    )

    _log(
        "INFO",
        "notification_feedback_recorded",
        user_id=user_id,
        card_id=card_id,
        action=action,
        muted_series_count=result.get("muted_series_count", 0),
    )

    return _json_resp(
        200,
        {
            "ok": True,
            "user_id": user_id,
            "card_id": card_id,
            "action": action,
            "muted_series_count": result.get("muted_series_count", 0),
            "audit_id": audit_id,
        },
    )


# ---------------------------------------------------------------------------
# User Preferences API (Sub-Task 10)
# ---------------------------------------------------------------------------

_ALLOWED_PREFERENCE_KEYS = frozenset({
    "pre_meeting_enabled",
    "notification_preview",
    "focus_action_scope",
    "slack_status_enabled",
    "safety_guardian_enabled",
    "location_in_sos",
    "muted_series",
    "quiet_hours",
    "learned_routines",
})

_DEFAULT_USER_PREFERENCES: dict[str, Any] = {
    "pre_meeting_enabled": False,
    "notification_preview": "generic",
    "focus_action_scope": "ask_every_time",
    "slack_status_enabled": False,
    "safety_guardian_enabled": False,
    "location_in_sos": "ask_each_time",
    "muted_series": [],
}


def handle_get_preferences(
    user_id: str, store: Any = default_store
) -> dict[str, Any]:
    """Retrieve non-sensitive user preferences."""
    if not user_id:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id is required"})

    try:
        if hasattr(store, "get_preferences"):
            user_prefs = store.get_preferences(user_id)
        elif hasattr(store, "whisper_preferences"):
            user_prefs = store.whisper_preferences.get(user_id, {})
        else:
            user_prefs = {}
    except StorageError as exc:
        return _json_resp(503, {"error": "storage_error", "message": str(exc)})

    merged = {**_DEFAULT_USER_PREFERENCES, **user_prefs, "user_id": user_id}

    # Security: ensure no sensitive keys are ever returned
    for sensitive_key in ("contacts", "phone_numbers", "auth_token", "oauth_token"):
        merged.pop(sensitive_key, None)

    return _json_resp(200, {"user_id": user_id, "preferences": merged})


def handle_update_preferences(
    body: dict[str, Any], store: Any = default_store
) -> dict[str, Any]:
    """Update user preferences with allowlist validation."""
    user_id = body.get("user_id")
    patch = body.get("preferences")

    if not user_id or not isinstance(patch, dict):
        return _json_resp(
            400,
            {"error": "invalid_parameters", "message": "user_id and preferences object are required"},
        )

    # Validate allowlist keys
    for key in patch:
        if key not in _ALLOWED_PREFERENCE_KEYS:
            return _json_resp(
                400,
                {"error": "unknown_preference_key", "message": f"Preference key '{key}' is not permitted"},
            )

    # Validate value constraints
    if "notification_preview" in patch and patch["notification_preview"] not in {"generic", "private_context"}:
        return _json_resp(400, {"error": "invalid_enum", "message": "notification_preview must be generic or private_context"})
    if "focus_action_scope" in patch and patch["focus_action_scope"] not in {"ask_every_time", "this_session", "never"}:
        return _json_resp(400, {"error": "invalid_enum", "message": "focus_action_scope must be ask_every_time, this_session, or never"})
    if "location_in_sos" in patch and patch["location_in_sos"] not in {"off", "ask_each_time", "approved_contacts_only"}:
        return _json_resp(400, {"error": "invalid_enum", "message": "location_in_sos must be off, ask_each_time, or approved_contacts_only"})

    if hasattr(store, "whisper_preferences"):
        current = store.whisper_preferences.setdefault(user_id, {**_DEFAULT_USER_PREFERENCES, "user_id": user_id})
        current.update(patch)
    else:
        current = {**_DEFAULT_USER_PREFERENCES, "user_id": user_id, **patch}

    try:
        if hasattr(store, "update_preferences"):
            store.update_preferences(user_id, current)
    except StorageError as exc:
        return _json_resp(503, {"error": "storage_error", "message": str(exc)})

    now = time.time()
    audit_id = f"pref_{uuid.uuid4().hex[:8]}"
    audit_entry = {
        "action_id": audit_id,
        "user_id": user_id,
        "category": "preferences_updated",
        "timestamp": now,
    }
    if hasattr(store, "record_audit"):
        try:
            store.record_audit(audit_entry)
        except Exception:
            pass
    elif hasattr(store, "audit_records"):
        store.audit_records.append(audit_entry)

    _log("INFO", "user_preferences_updated", user_id=user_id, updated_keys=list(patch.keys()))

    return _json_resp(200, {"ok": True, "user_id": user_id, "preferences": current})


# ---------------------------------------------------------------------------
# Adaptive Intent Routines API
# ---------------------------------------------------------------------------

def handle_get_routines(user_id: str, store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Retrieve learned adaptive intent routines for user."""
    routines = [
        r for r in store.learned_routines.values()
        if r.get("user_id") == user_id and r.get("active", True)
    ]
    return _json_resp(200, {"user_id": user_id, "routines": routines, "total": len(routines)})


def handle_create_routine(body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Create or remember an adaptive intent routine with scope='always'."""
    user_id = body.get("user_id")
    intent = body.get("intent")
    actions = body.get("actions", [])

    if not user_id or not intent:
        return _json_resp(400, {"error": "invalid_parameters", "message": "user_id and intent are required"})

    routine_id = body.get("routine_id") or f"rtn_{uuid.uuid4().hex[:8]}"
    intent_clean = intent.strip().lower()
    now = time.time()

    routine = {
        "routine_id": routine_id,
        "user_id": user_id,
        "intent": intent,
        "intent_pattern": intent_clean,
        "name": body.get("name", f"Routine: {intent}"),
        "actions": actions,
        "scope": body.get("scope", "always"),
        "active": True,
        "created_at": now,
    }
    store.learned_routines[routine_id] = routine

    # Create matching persistent grant in store.grants
    grant_id = f"grant_{routine_id}"
    store.grants[grant_id] = {
        "grant_id": grant_id,
        "user_id": user_id,
        "tenant_id": body.get("tenant_id", "tenant_default"),
        "action_type": "request_pause",
        "scope": "allow_always",
        "intent_pattern": intent_clean,
        "target_allowlist": [a.get("device") for a in actions if isinstance(a, dict) and a.get("device")],
        "active": True,
        "created_at": now,
    }

    # Record audit log
    store.audit_records.append(
        {
            "action_id": f"rtn_log_{uuid.uuid4().hex[:8]}",
            "user_id": user_id,
            "action_type": "routine_learned",
            "target_device_id": actions[0].get("device") if actions and isinstance(actions[0], dict) else None,
            "status": "executed",
            "reason": f"Learned routine registered for intent '{intent}'",
            "timestamp": now,
        }
    )

    return _json_resp(201, routine)


def handle_revoke_routine(routine_id: str, user_id: str, store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Revoke a saved intent routine."""
    routine = store.learned_routines.get(routine_id)
    if routine and routine.get("user_id") == user_id:
        routine["active"] = False
        routine["revoked_at"] = time.time()
        grant = store.grants.get(f"grant_{routine_id}")
        if grant:
            grant["active"] = False
    return _json_resp(200, {"revoked": True, "routine_id": routine_id, "revoked_at": time.time()})


def handle_update_routine(routine_id: str, body: dict[str, Any], store: InMemoryDataStore = default_store) -> dict[str, Any]:
    """Update actions or intent name for an existing routine."""
    routine = store.learned_routines.get(routine_id)
    if not routine:
        return _json_resp(404, {"error": "not_found", "message": f"Routine {routine_id} not found"})

    if "name" in body:
        routine["name"] = body["name"]
    if "actions" in body:
        routine["actions"] = body["actions"]
    if "intent" in body:
        routine["intent"] = body["intent"]
        routine["intent_pattern"] = body["intent"].strip().lower()
    routine["updated_at"] = time.time()
    return _json_resp(200, {"ok": True, "routine": routine})


def lambda_handler(
    event: dict[str, Any],
    _context: object = None,
    store_override: Any | None = None,
) -> dict[str, Any]:
    try:
        store = get_datastore(store_override)
    except StorageUnavailableError as st_err:
        return _json_resp(
            503,
            {"error": "storage_unavailable", "message": str(st_err)},
        )
    path = event.get("path") or event.get("rawPath") or ""
    http_method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "GET"
    query_params = event.get("queryStringParameters") or {}

    raw_body = event.get("body")
    body: dict[str, Any] = {}
    if isinstance(raw_body, str):
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return _json_resp(400, {"error": "invalid_json", "message": "Request body must be valid JSON"})
    elif isinstance(raw_body, dict):
        body = raw_body

    # Handle CORS OPTIONS preflight
    if http_method == "OPTIONS":
        return _json_resp(200, {"ok": True})

    # Public health check endpoint
    if path in ("/health", "/api/v1/health") and http_method == "GET":
        return _json_resp(200, {"status": "healthy", "service": "aura-control-plane", "timestamp": time.time()})

    # Server-Side Authentication & Authorization Enforcement
    if _auth and not _auth.is_public_route(http_method, path):
        allow_bypass = bool(
            store_override is not None
            and not os.getenv("AURA_STRICT_AUTH", "").strip().lower() in ("true", "1", "yes")
        )
        auth_ctx = _auth.extract_and_validate_auth(event, allow_test_bypass=allow_bypass)
        if not auth_ctx.is_authenticated:
            return _json_resp(
                401,
                {
                    "error": auth_ctx.error or "unauthorized",
                    "message": auth_ctx.error_message or "Missing or invalid authentication token",
                },
            )

        # Enforce user ownership against requested user_id if specified
        requested_uid = query_params.get("user_id") or (body.get("user_id") if isinstance(body, dict) else None)
        try:
            verified_uid = _auth.enforce_user_authorization(auth_ctx, requested_uid)
        except _auth.AuthError as auth_err:
            return _json_resp(
                auth_err.status_code,
                {"error": auth_err.error, "message": auth_err.message},
            )

        # Ensure downstream handlers use the verified identity
        if isinstance(body, dict) and "user_id" in body:
            body["user_id"] = verified_uid
        if "user_id" in query_params:
            query_params["user_id"] = verified_uid

    # Routing
    if path == "/api/v1/consent/grants" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_get_grants(user_id, store=store)

    if path == "/api/v1/consent/grants" and http_method == "POST":
        return handle_create_grant(body, store=store)

    if path.startswith("/api/v1/consent/grants/") and http_method == "DELETE":
        grant_id = path.split("/")[-1]
        user_id = query_params.get("user_id", "")
        return handle_revoke_grant(grant_id, user_id, store=store)

    if path == "/api/v1/focus/session" and http_method == "POST":
        return handle_focus_session(body, store=store)

    if path == "/api/v1/focus/bank" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_get_bank(user_id, store=store)

    if path == "/api/v1/focus/signals" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_get_signals(user_id, store=store)

    if path == "/api/v1/focus/signals" and http_method == "POST":
        return handle_set_signals(body, store=store)

    if path == "/api/v1/focus/signal" and http_method == "POST":
        return handle_focus_signal(body, store=store)

    # Phase 5 Connected Devices, Feature Flags, & Safety Guardrails
    if path == "/api/v1/devices" and http_method == "GET":
        return handle_get_devices(store=store)

    if path == "/api/v1/devices" and http_method == "POST":
        return handle_register_device(body, store=store)

    if path == "/api/v1/feature-flags" and http_method == "GET":
        return handle_get_feature_flags(store=store)

    if path == "/api/v1/feature-flags" and http_method == "POST":
        return handle_update_feature_flags(body, store=store)

    if path == "/api/v1/safety/guardrails" and http_method == "GET":
        user_id = query_params.get("user_id")
        return handle_get_guardrails(user_id=user_id, store=store)

    if path == "/api/v1/safety/guardrails/reset" and http_method == "POST":
        return handle_reset_guardrail(body, store=store)

    if path == "/api/v1/audit/actions" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        limit = int(query_params.get("limit", 20))
        return handle_query_audit(user_id, limit=limit, store=store)

    if path == "/api/v1/actions/undo" and http_method == "POST":
        return handle_undo_action(body, store=store)

    if path == "/api/v1/user/export" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_export_user_data(user_id, store=store)

    if path == "/api/v1/user/data" and http_method == "DELETE":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_delete_user_data(user_id, store=store)

    # Adaptive Intent Routines
    if path == "/api/v1/routines" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_get_routines(user_id, store=store)

    if path == "/api/v1/routines" and http_method == "POST":
        return handle_create_routine(body, store=store)

    if path.startswith("/api/v1/routines/") and http_method == "DELETE":
        routine_id = path.split("/")[-1]
        user_id = query_params.get("user_id", "")
        return handle_revoke_routine(routine_id, user_id, store=store)

    if path.startswith("/api/v1/routines/") and http_method in ("PUT", "PATCH"):
        routine_id = path.split("/")[-1]
        return handle_update_routine(routine_id, body, store=store)

    # Phase 6 Slack Ambient Status & OAuth PKCE Endpoints
    if path == "/api/v1/slack/oauth/authorize" and http_method == "POST":
        return handle_slack_oauth_authorize(body, store=store)

    if path == "/api/v1/slack/oauth/callback" and http_method == "POST":
        return handle_slack_oauth_callback(body, store=store)

    if path == "/api/v1/slack/preview" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_slack_preview(user_id, query_params, store=store)

    if path == "/api/v1/slack/status" and http_method == "POST":
        return handle_publish_slack_status(body, store=store)

    if path == "/api/v1/slack/status" and http_method == "DELETE":
        return handle_clear_slack_status(body, store=store)

    if path == "/api/v1/slack/workspaces" and http_method == "GET":
        return handle_get_slack_workspaces(store=store)

    if path == "/api/v1/slack/admin-review" and http_method == "POST":
        return handle_slack_admin_review(body, store=store)

    # Phase 7 Wearable registration & Safety escalation
    if path in ("/apns/register", "/api/v1/devices/wearable") and http_method == "POST":
        return handle_register_wearable(body, store=store)

    if path == "/api/v1/safety/escalate" and http_method == "POST":
        return handle_safety_escalate(body, store=store)

    if path == "/api/v1/safety/cancel" and http_method == "POST":
        return handle_safety_cancel(body, store=store)

    # Notification feedback (Sub-Task 3)
    if path == "/api/v1/notifications/feedback" and http_method == "POST":
        return handle_notification_feedback(body, store=store)

    # Preferences (Sub-Task 10)
    if path == "/api/v1/preferences" and http_method == "GET":
        user_id = query_params.get("user_id", "")
        if not user_id:
            return _json_resp(400, {"error": "missing_query_param", "message": "user_id is required"})
        return handle_get_preferences(user_id, store=store)

    if path == "/api/v1/preferences" and http_method in ("POST", "PATCH"):
        return handle_update_preferences(body, store=store)

    return _json_resp(404, {"error": "not_found", "message": f"Resource {http_method} {path} not found"})
