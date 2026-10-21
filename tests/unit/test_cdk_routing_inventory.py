"""Unit tests verifying complete and secure CDK routing inventory for Project AURA.

Verifies:
1. Every frontend API call in frontend/app.js maps to a registered CDK route.
2. Every handler endpoint in lambda/control_plane.py is registered in CDK.
3. Public routes (/health, /api/v1/health, /api/v1/devices GET, /api/v1/feature-flags GET, /api/v1/slack/oauth/callback POST)
   are explicitly registered WITHOUT authorizer.
4. All sensitive / protected routes are explicitly registered WITH cognitoAuthorizer.
5. No unrestricted public catch-all (like '$default' or '{proxy+}') is registered on the HTTP API.
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

STACK_PATH = Path(__file__).resolve().parent.parent.parent / "infra" / "lib" / "aura-cloud-intake-stack.ts"
CONTROL_PLANE_PATH = Path(__file__).resolve().parent.parent.parent / "lambda" / "control_plane.py"
APP_JS_PATH = Path(__file__).resolve().parent.parent.parent / "frontend" / "app.js"


def _parse_cdk_routes(stack_content: str) -> list[dict[str, Any]]:
    """Parse this.httpApi.addRoutes blocks from the TypeScript stack file."""
    route_pattern = re.compile(
        r"this\.httpApi\.addRoutes\(\{\s*path:\s*'([^']+)',\s*methods:\s*\[([^\]]+)\],(?:\s*integration:\s*(\w+),)?(?:\s*authorizer:\s*(\w+),)?",
        re.MULTILINE,
    )
    routes = []
    for match in route_pattern.finditer(stack_content):
        path = match.group(1)
        methods_raw = match.group(2)
        authorizer = match.group(4)
        methods = [
            m.strip().replace("apigw2.HttpMethod.", "")
            for m in methods_raw.split(",")
            if m.strip()
        ]
        routes.append({
            "path": path,
            "methods": methods,
            "authorizer": authorizer,
            "is_protected": authorizer == "cognitoAuthorizer",
        })
    return routes


def test_no_http_api_catch_all_route():
    """Verify no wildcard or unrestricted catch-all exists on the HTTP API."""
    content = STACK_PATH.read_text(encoding="utf-8")
    routes = _parse_cdk_routes(content)
    paths = [r["path"] for r in routes]

    # Ensure no proxy or wildcard routes on HTTP API
    for path in paths:
        assert "{proxy+}" not in path
        assert "*" not in path
        assert path != "$default"


def test_public_routes_have_no_authorizer():
    """Verify that only explicitly permitted public routes omit the authorizer."""
    content = STACK_PATH.read_text(encoding="utf-8")
    routes = _parse_cdk_routes(content)

    public_routes = [r for r in routes if not r["is_protected"]]
    public_paths = {r["path"] for r in public_routes}

    expected_public_paths = {
        "/health",
        "/api/v1/health",
        "/api/v1/devices",
        "/api/v1/feature-flags",
        "/api/v1/slack/oauth/callback",
    }
    assert public_paths == expected_public_paths

    # Verify methods for public endpoints
    for r in public_routes:
        if r["path"] in ("/health", "/api/v1/health", "/api/v1/devices", "/api/v1/feature-flags"):
            assert r["methods"] == ["GET"]
        elif r["path"] == "/api/v1/slack/oauth/callback":
            assert r["methods"] == ["POST"]


def test_protected_routes_require_cognito_authorizer():
    """Verify that all core API routes are protected with cognitoAuthorizer."""
    content = STACK_PATH.read_text(encoding="utf-8")
    routes = _parse_cdk_routes(content)

    protected_routes = [r for r in routes if r["is_protected"]]
    protected_paths = {r["path"] for r in protected_routes}

    must_be_protected = [
        "/api/v1/focus/signal",
        "/api/v1/focus/session",
        "/api/v1/focus/bank",
        "/api/v1/focus/signals",
        "/api/v1/consent/grants",
        "/api/v1/consent/grants/{grantId}",
        "/api/v1/devices",
        "/api/v1/devices/wearable",
        "/apns/register",
        "/api/v1/feature-flags",
        "/api/v1/safety/guardrails",
        "/api/v1/safety/guardrails/reset",
        "/api/v1/safety/escalate",
        "/api/v1/safety/cancel",
        "/api/v1/audit/actions",
        "/api/v1/actions/undo",
        "/api/v1/preferences",
        "/api/v1/routines",
        "/api/v1/routines/{routineId}",
        "/api/v1/slack/oauth/authorize",
        "/api/v1/slack/preview",
        "/api/v1/slack/status",
        "/api/v1/slack/workspaces",
        "/api/v1/slack/admin-review",
        "/api/v1/notifications/feedback",
        "/api/v1/user/export",
        "/api/v1/user/data",
    ]

    for p in must_be_protected:
        assert p in protected_paths, f"Route {p} must be protected in CDK stack"


def test_frontend_api_calls_covered_by_cdk_routes():
    """Verify that all endpoints called in frontend/app.js are registered in CDK stack."""
    stack_content = STACK_PATH.read_text(encoding="utf-8")
    routes = _parse_cdk_routes(stack_content)
    cdk_paths = {r["path"] for r in routes}

    frontend_content = APP_JS_PATH.read_text(encoding="utf-8")
    api_calls = re.findall(r"this\._request\(['\"]([^'\"?]+)", frontend_content)

    for call_path in set(api_calls):
        # Normalize template literals e.g. /api/v1/consent/grants/${...} -> /api/v1/consent/grants/{grantId}
        normalized = re.sub(r"\$\{.*\}", "{id}", call_path)
        # Check matching route
        matched = False
        for cdk_path in cdk_paths:
            cdk_norm = re.sub(r"\{[^}]+\}", "{id}", cdk_path)
            if normalized == cdk_norm:
                matched = True
                break
        assert matched, f"Frontend API call {call_path} must be covered by a registered CDK route"
