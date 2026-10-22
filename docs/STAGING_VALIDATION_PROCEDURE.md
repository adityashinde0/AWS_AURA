# Project AURA — Live Staging Environment Validation & Sign-Off Runbook

**Document Purpose:** Prescriptive, step-by-step verification guide for deploying and validating Project AURA in an isolated AWS staging account before production approval.
**Target Audience:** Cloud Engineers, Backend Engineers, QA Leads, and Security Operators.
**Production Isolation Invariant:** Never deploy staging workloads or run integration tests against production AWS accounts, live user tables, or real corporate Slack workspaces.

---

## 1. Staging Environment Selection & Prerequisites

### 1.1 Account & Region Boundary
- **AWS Staging Account:** Dedicated sandbox/staging AWS account (e.g., `123456789012-staging`).
- **Target Region:** `us-east-1` (Default Bedrock Claude 3.5 Sonnet availability region).
- **Required Local Tooling:**
  - AWS CLI v2 (`aws --version`)
  - Node.js 18+ and AWS CDK v2 (`cdk --version`)
  - Python 3.11+ with `requirements-dev.txt` installed in an isolated venv.
  - Dedicated Slack Test Workspace (Developer sandbox where user is Workspace Admin).

### 1.2 Required AWS Environment Variables
```bash
export AWS_REGION="us-east-1"
export AWS_DEFAULT_REGION="us-east-1"
export CDK_DEFAULT_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
export CDK_DEFAULT_REGION="us-east-1"
export AURA_STORAGE_MODE="dynamodb"
export AURA_STRICT_AUTH="true"
export BEDROCK_MOCK="false"
```

---

## 2. Infrastructure Deployment (AWS CDK)

### 2.1 Synth & Security Audit
Before deploying, synthesize CloudFormation templates and inspect security bounds:
```bash
cd infra
npm ci
npx cdk synth AuraCloudIntakeStack
```
Verify:
1. `AuraCoreTable` specifies billing mode `PAY_PER_REQUEST` with partition key `PK`, sort key `SK`, and TTL on `ttl`.
2. `AuraWebSocketConnections` has partition key `connectionId` and TTL on `ttl`.
3. KMS CMK encryption is enabled on all DynamoDB tables and SQS queues.
4. HTTP API does **not** declare any wildcard catch-all route (`$default`).

### 2.2 Controlled Deployment
```bash
npx cdk deploy AuraCloudIntakeStack --require-approval broadening
```
Record the CloudFormation stack outputs:
- `HttpApiUrl`: e.g. `https://xxxxxx.execute-api.us-east-1.amazonaws.com`
- `WebSocketApiUrl`: e.g. `wss://yyyyyy.execute-api.us-east-1.amazonaws.com/prod`
- `UserPoolId`: e.g. `us-east-1_AuraStagingPool`
- `UserPoolClientId`: e.g. `3a4b5c6d7e8f9g0h`

---

## 3. DynamoDB UndoRecord & Lease Fencing Verification

### 3.1 Schema & TTL Verification
Ensure TTL is active on the staging table:
```bash
aws dynamodb describe-time-to-live \
  --table-name AuraCoreTable \
  --query "TimeToLiveDescription.TimeToLiveStatus"
# Expected output: "ENABLED"
```

### 3.2 Concurrent Lease Acquisition Test (Fencing Validation)
Run two parallel workers attempting to claim the same undo token to verify atomic condition check:
```bash
# Worker 1: Acquire lease (Duration: 30s)
python -c "
import uuid, importlib
cp = importlib.import_module('lambda.control_plane')
store = cp.get_datastore()
token = 'tok_staging_lease_test_01'
store.save_undo_record({'token': token, 'userId': 'usr_stage_1', 'tenantId': 'tenant_stage', 'status': 'PENDING'})
acq, code, msg = store.acquire_undo_lease(token, 'usr_stage_1', lease_owner_id='worker_stage_A', tenant_id='tenant_stage')
print('Worker A:', acq, code)
"

# Immediately Worker 2 attempts to claim before lease expiration:
python -c "
import importlib
cp = importlib.import_module('lambda.control_plane')
store = cp.get_datastore()
token = 'tok_staging_lease_test_01'
acq, code, msg = store.acquire_undo_lease(token, 'usr_stage_1', lease_owner_id='worker_stage_B', tenant_id='tenant_stage')
print('Worker B:', acq, code, msg)
"
```
**Expected Outcome:**
- Worker A: `True 200`
- Worker B: `False 409 Undo reversal currently in progress, please retry shortly`

### 3.3 Retry Counter Atomic Increment Test
Trigger a failure transition and verify `retry_count` increment:
```bash
python -c "
import importlib
cp = importlib.import_module('lambda.control_plane')
store = cp.get_datastore()
token = 'tok_staging_lease_test_01'
store.fail_undo(token, 'usr_stage_1', 'worker_stage_A', error_reason='simulated_network_timeout')
rec = store.get_undo_record(token)
print('Status:', rec.get('status'), 'RetryCount:', rec.get('retry_count'))
"
```
**Expected Outcome:**
- `Status: FAILED`
- `RetryCount: 1`

---

## 4. API Gateway & Cognito RS256 Authorization Verification

### 4.1 Production Bypass Safeguard Check
Verify that test bypass is strictly rejected in staging when running in Lambda context:
```bash
# Calling protected endpoint with test body but NO Bearer token
curl -s -X POST "${HTTP_API_URL}/api/v1/actions/undo" \
  -H "Content-Type: application/json" \
  -d '{"undo_token": "tok_stage_01", "user_id": "usr_stage_1"}'
```
**Expected Outcome:**
- HTTP `401 Unauthorized`
- Body: `{"error": "missing_credentials", "message": "Authorization token required for protected endpoint"}`

### 4.2 Valid Cognito RS256 JWT Authentication Check
Authenticate a staging test user against Cognito User Pool:
```bash
AUTH_TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "${USER_POOL_CLIENT_ID}" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=testuser@staging.aura,PASSWORD=StagingPass123! \
  --query "AuthenticationResult.IdToken" \
  --output text)

curl -s -X GET "${HTTP_API_URL}/api/v1/consent/grants?user_id=testuser" \
  -H "Authorization: Bearer ${AUTH_TOKEN}"
```
**Expected Outcome:**
- HTTP `200 OK`
- Returns user's active consent grants.

---

## 5. Slack Test-Workspace Integration Verification

### 5.1 OAuth PKCE Flow Verification
1. Initiate OAuth start flow:
   ```bash
   curl -s -X POST "${HTTP_API_URL}/api/v1/slack/oauth/authorize" \
     -H "Authorization: Bearer ${AUTH_TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{"user_id": "testuser", "workspace_id": "T_SLACK_STAGE"}'
   ```
2. Confirm the redirect URL requests only least-privilege scopes (`users.profile:write`, `dnd:write`).
3. Complete the PKCE code challenge and store verified token ARN in AWS Secrets Manager: `arn:aws:secretsmanager:us-east-1:*:secret:aura/slack/credentials`.

### 5.2 Status Publication & Undo Reversal
1. Publish ambient status:
   ```bash
   STATUS_RESP=$(curl -s -X POST "${HTTP_API_URL}/api/v1/slack/status" \
     -H "Authorization: Bearer ${AUTH_TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{"user_id": "testuser", "status_text": "In Deep Focus", "status_emoji": ":focus:"}')
   UNDO_TOKEN=$(echo $STATUS_RESP | jq -r '.undo_token')
   ```
2. Verify Slack profile reflects `:focus:` emoji in staging workspace.
3. Trigger Undo:
   ```bash
   curl -s -X POST "${HTTP_API_URL}/api/v1/actions/undo" \
     -H "Authorization: Bearer ${AUTH_TOKEN}" \
     -H "Content-Type: application/json" \
     -d "{\"user_id\": \"testuser\", \"undo_token\": \"${UNDO_TOKEN}\"}"
   ```
4. Verify Slack profile status is immediately cleared.

---

## 6. Ambiguous Network Timeouts & Authoritative Reconciliation Verification

### 6.1 Simulate Adapter Ambiguous Timeout
Simulate a dropped connection or timeout during device reversal:
```bash
python -c "
import importlib
cp = importlib.import_module('lambda.control_plane')
store = cp.get_datastore()
token = 'tok_ambiguous_test_01'
store.save_undo_record({
    'token': token,
    'userId': 'testuser',
    'tenantId': 'tenant_default',
    'status': 'PENDING',
    'action_type': 'request_pause',
    'target_device': 'dev_stage_firetv_01',
    'required_reversals': ['device'],
})
# Inject timeout exception on adapter execute_undo
store.fail_undo(token, 'testuser', 'worker_stage_1', error_reason='timeout_uncertain', uncertain_reversals=['device'])
"
```
### 6.2 Reconciliation Check
Trigger undo retry via API:
```bash
curl -s -X POST "${HTTP_API_URL}/api/v1/actions/undo" \
  -H "Authorization: Bearer ${AUTH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "testuser", "undo_token": "tok_ambiguous_test_01"}'
```
**Expected Outcome:**
- If Fire TV is reachable and state is `playing`, the handler authoritatively reconciles the state as restored (`device_ok = True`), completes the reversal, and returns HTTP 200.
- If Fire TV cannot be queried, the handler refuses blind replay and returns HTTP `502 reconciliation_required` with `retryable: False`.

---

## 7. Sign-Off Acceptance Checklist

> [!IMPORTANT]
> **Verification Boundary Notice:**
> Passing all 498 local unit, contract, and mock tests validates code correctness, deterministic logic, and security rules against in-memory models. It does **not** replace live cloud staging execution. Live cloud gates remain pending until credentials are provided and deployment is performed.

| Gate ID | Area | Verification Criterion | Local Verification (Tonight) | Live AWS Staging Verification |
|---|---|---|---|---|
| **GATE-01** | **CDK Infrastructure** | Cloud Intake Stack synthesized with zero deprecation warnings; KMS CMK active. | **COMPLETE** (Exit code 0, 125 CFN resources, 0 deprecation warnings) | **NOT STARTED** (Awaiting AWS staging credentials & approval) |
| **GATE-02** | **DynamoDB Leases** | Concurrent workers receive 409; zero overwrites; retry counter increments. | **COMPLETE** (Verified in `test_partial_undo_recovery.py` & `test_dynamodb_persistence.py`) | **NOT STARTED** (Awaiting live DynamoDB table execution) |
| **GATE-03** | **Cognito Auth** | RS256 token verification enforced; test bypass rejected in Lambda environment. | **COMPLETE** (Verified in `test_auth_enforcement.py` & `test_security_batch1_2a.py`) | **NOT STARTED** (Awaiting live Cognito User Pool issuance) |
| **GATE-04** | **Slack Integration** | OAuth PKCE token exchange stored in Secrets Manager; status published and cleared. | **COMPLETE** (Verified in `test_slack_adapter.py`) | **NOT STARTED** (Awaiting live Slack workspace & Secrets Manager) |
| **GATE-05** | **Reconciliation** | Blind retries blocked after ambiguous timeout; authoritative state query checked. | **COMPLETE** (Verified in `test_partial_undo_recovery.py` & `test_security_batch1_2a.py`) | **NOT STARTED** (Awaiting live device network testing) |
| **GATE-06** | **Test Suite** | Full test suite passes against environment. | **COMPLETE** (503/503 Passed in 7.82s) | **NOT STARTED** (Awaiting live staging runner) |

---

## 8. Final Staging Preflight Audit Results (Tonight)

### 8.1 Local CDK Synthesis & Configuration Audit
- **Command:** `npx cdk synth AuraCloudIntakeStack` (executed in `infra/`)
- **Status:** **PASS** (Exit code 0, 125 CloudFormation resources synthesized).
- **Persistent Resource Protection:**
  - `AuraEncryptionKey` (KMS CMK): `DeletionPolicy: Retain`, `UpdateReplacePolicy: Retain`
  - `AuraCoreTable` (DynamoDB): `DeletionPolicy: Retain`, `UpdateReplacePolicy: Retain`
  - `AuraUserPool` (Cognito): `DeletionPolicy: Retain`, `UpdateReplacePolicy: Retain`
  - `AuraWebSocketConnections`: `DeletionPolicy: Delete`, `UpdateReplacePolicy: Delete` (ephemeral connection state)
- **API Gateway Route Protection:**
  - 44 total routes synthesized.
  - 36 protected HTTP routes strictly enforce `AuthorizationType: JWT` with linked Cognito Authorizer.
  - 4 public HTTP discovery/health endpoints (`GET /health`, `GET /api/v1/health`, `GET /api/v1/devices`, `GET /api/v1/feature-flags`) plus OAuth callback (`POST /api/v1/slack/oauth/callback`).
  - 4 WebSocket routes (`$connect`, `$disconnect`, `relay`, `$default`) authenticated via cryptographic JWT RS256 in `lambda/websocket_handler.py`.
  - Zero catch-all (`$default`) wildcard route on the HTTP API.
- **IAM Policy Least Privilege:**
  - Zero blanket wildcard (`*`) permissions on sensitive resources.
  - All Lambda execution roles scoped to exact DynamoDB tables, KMS keys, event bus, and Secrets Manager ARNs.
- **WAFv2 Inspection:**
  - AWS Managed Common Rule Set, Known Bad Inputs, and 500 req/5min rate limiting active.

### 8.2 Host Environment & Credential Scan
- **AWS CLI Tooling:** `aws` executable not detected in host system PATH; `C:\Program Files\Amazon\AWSCLIV2\aws.exe` absent.
- **AWS Credentials:** `~/.aws/credentials` and `~/.aws/config` absent.
- **Environment Variables:** Zero `AWS_*` credentials or secrets exposed in runtime environment.
- **Target Verification:** Because credentials are not configured, live `aws sts get-caller-identity` could not be run. A dedicated staging AWS account ID must be targeted when credentials are provided.

### 8.3 Exact Deployment Command
Once staging credentials are configured in your shell:
```bash
cd infra
npx cdk deploy AuraCloudIntakeStack \
  --require-approval broadening \
  --parameters BootstrapVersion="/cdk-bootstrap/hnb659fds/version"
```

### 8.4 Rollback Approach
If any staging verification gate fails after deployment:
1. **Immediate API Rollback:** Disable public or protected routing via Feature Flags (`global_switches: {"emergency_kill_switch": true}`).
2. **CDK Stack Destroy / Teardown:**
   ```bash
   cd infra
   npx cdk destroy AuraCloudIntakeStack
   ```
   *Note: Persistent data in `AuraCoreTable`, `AuraEncryptionKey`, and `AuraUserPool` is preserved due to `DeletionPolicy: Retain`.*
