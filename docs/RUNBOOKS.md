# Project AURA — Production Operations & Incident Response Runbooks

This operational handbook provides tier-1 and tier-2 on-call engineers with prescriptive mitigation procedures for Project AURA. Every procedure preserves the foundational privacy invariant: **Zero raw transcripts, zero covert surveillance, and zero meeting leakage to third parties.**

---

## Quick Reference: Emergency Command Matrix

| Scenario | Primary Blast Radius | Immediate Action | Mitigation Tool |
|---|---|---|---|
| **1. Bee Outage** | Local companion SSE stream | Fail safe to offline local queue; mobile notification fallback | `bee status`, local outbox buffer |
| **2. Calendar Outage** | Pre-meeting T-5 briefings | Skip T-5 triggers gracefully; disable calendar sync | Feature Flag: `pre_meeting_briefing = False` |
| **3. Model Outage** | Bedrock Claude 3.5 inference | Fallback to deterministic template cards (0 LLM reliance) | Bedrock safe fallback handler |
| **4. Slack / Alexa Outage** | Slack ambient status & device pause | Trip tenant/global kill switch; queue outbox with TTL | Feature Flag: `slack_post = False`, `device = False` |
| **5. Compromised Token** | OAuth or AWS KMS credentials | Immediate cryptographic revocation in Secrets Manager (<60s) | AWS Secrets Manager secret rotation |
| **6. GDPR Purge Request** | Individual user data footprint | Execute synchronous cryptographic erasure across all partitions | `/api/v1/user/data` (DELETE) |
| **7. Flapping / Rogue Action** | Rapid unprompted device commands | Trip Global Kill Switch; PRD >5% undo rate circuit breaker | `/api/v1/feature-flags` (`global_switches`) |

---

## Runbook 1: Bee Wearable / CLI / SSE Outage

### 1.1 Symptoms & Detection
- Local Companion logs report: `SSE connection closed (code 1006)` or `bee stream: connection refused`.
- CloudWatch metric: `BeeHeartbeatFailureCount > 3` in 5 minutes.
- User report: "No context briefings arriving before sync."

### 1.2 Impact
- **Severity:** Medium (Degraded functionality).
- Only local context extraction is impaired. Personal device boundary remains safe. Cloud intake rejects empty context envelopes safely.

### 1.3 Resolution Steps
1. **Local Companion Diagnosis:**
   ```bash
   bee status
   bee ping
   bee today --context --json
   ```
2. **Restart Local Supervised Worker:**
   ```bash
   pkill -f bee_monitor.py
   python standalone_agent/bee_monitor.py --daemon
   ```
3. **Verify Safe Outbox Drainage:**
   Confirm local outbox buffer (`~/.aura/outbox.db`) does not drop events during downtime. On reconnect, the outbox drains idempotently using monotonically incrementing event IDs.
4. **Fallback:** If Bee hardware remains offline, AURA automatically downgrades to Google/Outlook calendar metadata without wearable context.

---

## Runbook 2: Calendar Provider (Google/Outlook) Outage & Sync Drift

### 1.1 Symptoms & Detection
- Calendar webhook returns `503 Service Unavailable` or `401 Token Expired`.
- CloudWatch Alarm: `CalendarSyncDriftMinutes > 15`.

### 1.2 Impact
- **Severity:** Low.
- Pre-meeting prep briefings are suppressed. Focus sessions and connected device controls continue unaffected.

### 1.3 Resolution Steps
1. **Verify Quiet Hours & Boundary Protection:**
   Confirm system didn't suppress events due to user's quiet hours (22:00–08:00 local time).
2. **Execute Calendar Scheduler Token Refresh:**
   Trigger OAuth token refresh for Google Calendar or Microsoft Graph API via IAM identity broker.
3. **Trigger Manual Catch-up Sync:**
   ```bash
   python standalone_agent/calendar_scheduler.py --resync --user usr_marcus
   ```
4. **Temporary Mitigation:** If upstream provider is down, disable pre-meeting triggers globally or per-tenant:
   ```json
   POST /api/v1/feature-flags
   { "tenant_id": "tenant_default", "capability": "pre_meeting_briefing", "enabled": false }
   ```

---

## Runbook 3: Amazon Bedrock / Claude 3.5 Model Outage

### 1.1 Symptoms & Detection
- CloudWatch Alarm: `BedrockThrottlingException` or `ModelTimeoutError > 5%`.
- Lambda logs: `bedrock-runtime:InvokeModel failed with 500/503`.

### 1.2 Impact
- **Severity:** Medium.
- Dynamic 3-question generation and tone inference are unavailable.

### 1.3 Resolution Steps
1. **Automated Multi-LLM Resilience Failover (Default Behavior):**
   Project AURA’s `MultiProviderModelGateway` ([`lambda/resilience_gateway.py`](../lambda/resilience_gateway.py)) automatically enforces wall-clock deadlines (5.0s Bedrock primary timeout, 8.0s secondary deadline). Upon detecting Bedrock timeouts (>5.0s), throttling (HTTP 429), or 5xx server errors, it automatically fails over and routes requests to **IBM Watsonx Granite 3.0** (`granite-3-8b-instruct`) without dropping the request.
2. **Verify Deterministic Safe Fallback Mode:**
   If both Bedrock and Watsonx are degraded, AURA automatically fails closed to a deterministic heuristic briefing (<20ms execution budget, zero external AI reliance):
   ```json
   {
     "prep_summary": "Upcoming calendar sync: [Title]. Grounded prep unavailable due to model maintenance.",
     "3_questions": ["What is the primary decision required?", "What are the key dependencies?", "Who owns the follow-up actions?"],
     "emotion_tone": "neutral",
     "recommended_action": "none",
     "reason": "Safe deterministic fallback active"
   }
   ```
3. **Manual Failover to Alternate Bedrock Model ID:**
   Primary deployed model is `anthropic.claude-3-5-sonnet-20241022-v2:0`. If operator intervention is desired to route directly to secondary Claude 3 Haiku:
   ```bash
   aws lambda update-function-configuration \
     --function-name aura-focus-guardian \
     --environment "Variables={BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0}"
   ```

---

## Runbook 4: Slack API or Alexa Smart Home Service Degradation

### 1.1 Symptoms & Detection
- CloudWatch Alarm: `SlackWebhookFailureRate > 2%` or `DeviceCommandTimeout > 5`.
- Slack returns `429 Too Many Requests` (Rate limit exceeded on `chat:write` or `users.profile:set`).

### 1.2 Impact
- **Severity:** Medium.
- Ambient team status or Echo/Fire TV device pauses fail to post. Focus sessions continue locally.

### 1.3 Resolution Steps
1. **Validate Zero Meeting Leakage Rule:**
   Verify no failed Slack payloads generated leakage alerts (`AURA-P1-ZeroLeakageRuleViolation`).
2. **Trip Capability Kill Switch:**
   Instantly pause Slack publishing or device dispatch while upstream resolves:
   ```bash
   curl -X POST https://api.aura.internal/v1/feature-flags \
     -H "Content-Type: application/json" \
     -d '{"capability": "slack_post", "enabled": false, "tier": "global"}'
   ```
3. **Inspect SQS Dead Letter Queue (DLQ):**
   Messages failed due to 429 rate limiting are quarantined in `AuraIngestionDLQ`.
4. **Execute Redrive Worker Once Upstream Recovers:**
   ```bash
   python -c "from standalone_agent.production_readiness import ResiliencySimulator; sim = ResiliencySimulator(); print(sim.redrive_dlq_messages())"
   ```

---

## Runbook 5: Compromised Token or Security Credential Incident Response

### 1.1 Symptoms & Detection
- Unauthorized API call detected with invalid IP or anomaly in AWS CloudTrail / WAF.
- Slack OAuth token reported compromised by security team.

### 1.2 Impact
- **Severity:** P0 / Critical.
- Risk of unauthorized ambient status changes. (Zero risk of meeting leakage since tokens only possess `chat:write` / `users.profile:write` least privilege).

### 1.3 Immediate Containment (<60 Seconds)
1. **Trip Global Slack Kill Switch:**
   ```bash
   curl -X POST https://api.aura.internal/v1/feature-flags \
     -H "Content-Type: application/json" \
     -d '{"capability": "slack_post", "enabled": false, "tier": "global"}'
   ```
2. **Rotate KMS Secrets Manager Secret:**
   ```bash
   aws secretsmanager rotate-secret \
     --secret-id aura/slack/token-T012345678 \
     --rotation-rules AutomaticallyAfterDays=0
   ```
3. **Revoke OAuth Grants via Control Plane:**
   ```bash
   curl -X DELETE "https://api.aura.internal/v1/consent/grants/grant_slack_status?user_id=ALL"
   ```
4. **Invalidate In-Memory Revocation Cache:**
   All Lambda worker instances purge cached grants within 60 seconds.

---

## Runbook 6: GDPR/CCPA Right-to-be-Forgotten Cryptographic Purge

### 1.1 Symptoms & Trigger
- DSR (Data Subject Request) ticket logged via compliance portal or user clicks "Cryptographic Purge" in Control Plane UI.

### 1.2 Compliance SLA
- AURA architecture fulfills right-to-be-forgotten requests **synchronously in <500ms**, far exceeding the 30-day statutory limit.

### 1.3 Execution Procedure
1. **Trigger Cryptographic Erasure API:**
   ```bash
   curl -X DELETE "https://api.aura.internal/v1/user/data?user_id=usr_marcus" \
     -H "Authorization: Bearer <AdminOIDC>"
   ```
2. **Automated Verification Checklist:**
   - [x] All DynamoDB items under `USER#usr_marcus` deleted.
   - [x] All consent grants under `TENANT#*#USER#usr_marcus` deleted.
   - [x] Deep Work Bank balance and transaction history purged.
   - [x] Active Slack status cleared and token reference severed.
   - [x] Immutable action audit records anonymized / unlinked.
   - [x] Zero raw transcripts existed in storage (verified by design).
3. **Export Compliance Receipt:**
   Deliver deletion receipt JSON (`deleted_at`, `purged_records`) to compliance officer.

---

## Runbook 7: Global Emergency Kill Switch & Post-Incident Safe Recovery

### 1.1 Symptoms & Trigger
- Multiple user complaints of flapping device actions (e.g. Fire TV pausing unexpectedly).
- CloudWatch Alarm: `AURA-P1-UndoRateCircuitBreakerTripped` (Undo rate exceeded 5.0%).

### 1.2 Immediate Isolation (<1 Second)
1. **Execute Tier-1 Global Kill Switch:**
   ```bash
   curl -X POST https://api.aura.internal/v1/feature-flags \
     -H "Content-Type: application/json" \
     -d '{"global_switches": {"request_pause": false, "request_dnd": false, "slack_post": false}}'
   ```
2. **Confirm Fail-Closed State:**
   - Any new incoming focus signal immediately returns `status: blocked_kill_switch`.
   - Control plane UI displays red banner: *"Emergency Kill Switch Active"*.
   - No device commands or Slack updates can execute anywhere in the system.

### 1.3 Triage & Root Cause Investigation
1. Inspect audit log for undo patterns:
   ```bash
   curl "https://api.aura.internal/v1/safety/guardrails?user_id=all"
   ```
2. Check if a specific device adapter was flapping or if an upstream sensor misclassified noise.
3. Review sliding-window rate limit violations in CloudWatch (`CommandRateLimitExceeded`).

### 1.4 Post-Incident Safe Recovery Procedure
1. Reset the PRD Section 6 Safety Circuit Breaker:
   ```bash
   curl -X POST https://api.aura.internal/v1/safety/guardrails/reset \
     -H "Content-Type: application/json" \
     -d '{"user_id": "all", "action_type": "all"}'
   ```
2. Re-enable capabilities progressively (staged rollout: 10% -> 50% -> 100%):
   ```bash
   curl -X POST https://api.aura.internal/v1/feature-flags \
     -H "Content-Type: application/json" \
     -d '{"rollout_percentage": 10, "global_switches": {"request_pause": true, "request_dnd": true, "slack_post": true}}'
   ```
3. Monitor CloudWatch for 30 minutes; ensure undo rate stays `< 1.0%` before lifting cohort restriction to 100%.

---

**Author / Architect:** Aditya Shinde  
*Project AURA · All Rights Reserved*
