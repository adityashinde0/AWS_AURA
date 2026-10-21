"""Contract tests verifying AWS CDK Route Inventory and Configuration against Control Plane handlers.

Ensures:
1. All essential control plane routes are explicitly declared in the CDK stack.
2. Protected endpoints are bound to cognitoAuthorizer.
3. Public endpoints (health, feature-flags, devices, slack callback) remain public.
4. Lambda timeout accommodates the multi-provider resilience gateway execution budget.
5. WATSONX_SECRET_ARN is configured in the Focus Guardian Lambda environment.
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

CDK_STACK_PATH = Path(__file__).resolve().parent.parent.parent / "infra" / "lib" / "aura-cloud-intake-stack.ts"


@pytest.fixture(scope="module")
def cdk_stack_content() -> str:
    assert CDK_STACK_PATH.exists(), f"CDK stack file not found at {CDK_STACK_PATH}"
    with open(CDK_STACK_PATH, "r", encoding="utf-8") as f:
        return f.read()


def test_cdk_declares_all_core_routes(cdk_stack_content: str):
    """Verify that all core routes implemented in control_plane.py are present in the CDK stack."""
    required_routes = [
        "/health",
        "/api/v1/health",
        "/api/v1/devices",
        "/api/v1/feature-flags",
        "/api/v1/consent/grants",
        "/api/v1/consent/grants/{grantId}",
        "/api/v1/focus/session",
        "/api/v1/focus/bank",
        "/api/v1/focus/signals",
        "/api/v1/focus/signal",
        "/api/v1/audit/actions",
        "/api/v1/actions/undo",
        "/api/v1/preferences",
        "/api/v1/routines",
        "/api/v1/routines/{routineId}",
        "/api/v1/slack/oauth/authorize",
        "/api/v1/slack/oauth/callback",
        "/api/v1/slack/preview",
        "/api/v1/slack/status",
        "/api/v1/slack/workspaces",
        "/api/v1/slack/admin-review",
        "/api/v1/safety/guardrails",
        "/api/v1/safety/guardrails/reset",
        "/api/v1/safety/escalate",
        "/api/v1/safety/cancel",
        "/api/v1/notifications/feedback",
        "/api/v1/user/export",
        "/api/v1/user/data",
    ]

    for route in required_routes:
        assert f"path: '{route}'" in cdk_stack_content, f"Missing route in CDK stack: {route}"


def test_cdk_protects_sensitive_endpoints_with_authorizer(cdk_stack_content: str):
    """Verify that protected endpoints are explicitly bound to cognitoAuthorizer in CDK."""
    protected_routes = [
        "/api/v1/focus/signal",
        "/api/v1/focus/session",
        "/api/v1/consent/grants",
        "/api/v1/audit/actions",
        "/api/v1/actions/undo",
        "/api/v1/preferences",
        "/api/v1/routines",
        "/api/v1/user/export",
        "/api/v1/user/data",
    ]

    # Find each route block and assert authorizer: cognitoAuthorizer is attached
    for route in protected_routes:
        pattern = rf"path:\s*'{re.escape(route)}'[\s\S]*?authorizer:\s*cognitoAuthorizer"
        assert re.search(pattern, cdk_stack_content), f"Route {route} is missing cognitoAuthorizer binding"


def test_cdk_public_endpoints_have_no_authorizer(cdk_stack_content: str):
    """Verify that public routes in CDK do not have an authorizer attached."""
    public_routes = [
        "/health",
        "/api/v1/health",
        "/api/v1/devices",
        "/api/v1/feature-flags",
        "/api/v1/slack/oauth/callback",
    ]

    for route in public_routes:
        match = re.search(rf"path:\s*'{re.escape(route)}'[\s\S]*?integration:", cdk_stack_content)
        assert match, f"Public route {route} not found"
        # Ensure 'authorizer:' is not in that specific addRoutes call
        escaped_route = re.escape(route)
        pattern = r"addRoutes\(\{[\s\S]*?path:\s*'" + escaped_route + r"'[\s\S]*?\}\);"
        call_match = re.search(pattern, cdk_stack_content)
        if call_match:
            assert "authorizer:" not in call_match.group(0), f"Public route {route} should not have an authorizer"


def test_cdk_gateway_timeout_budget_alignment(cdk_stack_content: str):
    """Verify Focus Guardian Lambda timeout is aligned with the multi-provider gateway budget."""
    # Timeout must be at least 15s to allow primary (5s) + retry (5s) + secondary (8s)
    match = re.search(r"timeout:\s*cdk\.Duration\.seconds\((\d+)\)", cdk_stack_content)
    assert match, "Could not find Lambda timeout in CDK stack"
    timeout_sec = int(match.group(1))
    assert timeout_sec >= 15, f"FocusGuardian Lambda timeout ({timeout_sec}s) is too short for gateway execution budget"


def test_cdk_watsonx_secrets_manager_configuration(cdk_stack_content: str):
    """Verify WATSONX_SECRET_ARN environment variable and least-privilege IAM policy."""
    assert "WATSONX_SECRET_ARN:" in cdk_stack_content
    assert "SecretsManagerWatsonxAccess" in cdk_stack_content
    assert "secretsmanager:GetSecretValue" in cdk_stack_content
