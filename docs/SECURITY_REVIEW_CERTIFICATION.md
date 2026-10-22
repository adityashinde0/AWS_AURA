# Project AURA — Connected Devices Certification & Security Review
**Document Classification:** Internal Production Review / GA Gate  
**Version:** 1.0.0  
**Phase:** Phase 5 — Connected Devices & Hardware Adapters  
**Status:** Approved for GA Staging  

---

## 1. Executive Summary

Project AURA's connected device subsystem enables privacy-first, reversible physical-environment interventions during focused work sessions. In accordance with AURA's Core Product Requirements ([`PRD.md`](PRD.md)) and Production Architecture ([`ARCHITECTURE.md`](ARCHITECTURE.md)), **no device command is ever executed without deterministic policy validation, verified certification contracts, household boundary checks, multi-tier kill switches, and cryptographic undo tokens.**

This document provides the formal security architecture review and Amazon certification compliance specification for:
1. **Amazon Fire TV MediaSession API Integration** (Certified transport control for media pause/resume)
2. **Amazon Alexa / Echo Do Not Disturb (DND) Integration** (Certified Alexa Smart Home Skill directive)
3. **Safety Guardrail Circuit Breakers** (Automatic auto-execution pause if undo rate exceeds 5%)
4. **STRIDE Threat Model & Zero Raw Media Boundary**

---

## 2. Amazon Fire TV MediaSession API Integration Review

### 2.1 Certification Scope & Compliant Control Paths
Fire TV applications expose media playback control to the Android OS and Alexa voice engine through the standard Android `MediaSession` / `MediaSessionCompat` framework. AURA integrates strictly via documented, certified interfaces:

- **Compliant Interface:** Android `MediaSessionManager` and `MediaControllerCompat.TransportControls`
  - Action Pause: `TransportControls.pause()` emitting `PlaybackStateCompat.ACTION_PAUSE`
  - Action Resume (Undo): `TransportControls.play()` emitting `PlaybackStateCompat.ACTION_PLAY`
- **Official Amazon Reference:** [Amazon Developer Fire TV MediaSession Integration](https://developer.amazon.com/docs/fire-tv/mediasession-api-integration.html)

### 2.2 Prohibited Remote Control Mechanisms
To pass Amazon Developer Certification and protect user security:
- **No ADB Shell Access:** AURA strictly prohibits any Android Debug Bridge (ADB) network connection or arbitrary `input keyevent` injection.
- **No Blanket Remote Control:** AURA does not power cycle devices, launch arbitrary URLs, or alter device settings outside authenticated media playback.
- **Fail Safe on Uncertified Targets:** Non-certified smart displays or legacy HDMI-CEC hardware degrade to user-facing suggestions (`ask_user_confirmation`), never automated background execution.

---

## 3. Amazon Alexa Smart Home Skill & Echo DND Review

### 3.1 Certification Scope & Compliant Interfaces
Alexa Smart Home capabilities for Echo speakers are governed by the Alexa Skills Kit (ASK) Smart Home API:

- **Compliant Interface:** `Alexa.DoNotDisturb` Interface v3
  - Directives: `SetDoNotDisturb` and `ClearDoNotDisturb`
  - Endpoint Health: `Alexa.EndpointHealth` reports connectivity and reachability
- **Authentication:** OAuth 2.0 PKCE Account Linking with token exchange hosted in AWS Secrets Manager. Tokens are never stored in plain text or client-side bundles.
- **Official Amazon Reference:** [Alexa Smart Home API Documentation](https://developer.amazon.com/docs/smarthome/understand-the-smart-home-skill-api.html)

### 3.2 Household Boundary Isolation
- Every Echo device belongs to an Amazon Household identifier (`household_id`).
- When Focus Guardian proposes a DND toggle, AURA enforces strict match between the requesting user's household and the target device's registered household.
- **Cross-Household Rejection:** Any directive spanning across disparate households is rejected immediately with `blocked_household_mismatch` and logged in the immutable audit trail.

---

## 4. Multi-Tier Feature Flags & Emergency Kill Switches

To guarantee operational resilience and blast-radius minimization, AURA implements a 3-tier hierarchy ([`feature_flags.py`](../standalone_agent/feature_flags.py)):

```
┌────────────────────────────────────────────────────────┐
│               Global Kill Switch (Tier 1)              │
│       Instant disable of any capability system-wide    │
└───────────────────────────┬────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────┐
│               Tenant Kill Switch (Tier 2)              │
│    Per-organization / per-tenant capability gating    │
└───────────────────────────┬────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────┐
│             User Feature Flags / Rollout (Tier 3)      │
│      Cohort percentage rollout & explicit user opt-in   │
└────────────────────────────────────────────────────────┘
```

1. **Global Kill Switch:** An operator can disable `request_pause` or `request_dnd` globally with zero deployment latency. All evaluations fail closed to `blocked_kill_switch`.
2. **Tenant Kill Switch:** Enterprises can restrict device controls or Slack integrations across their organizational boundary.
3. **Cohort Rollout:** Progressive rollouts (e.g. 10% → 50% → 100%) use deterministic MD5 hashing on `user_id` to guarantee stable cohort assignment.

---

## 5. Safety Guardrail: Automatic Auto-Execution Circuit Breaker

In compliance with **PRD Section 6 Guardrails**:
> *"Undo rate > 5% for a policy pauses auto-execution."*

### 5.1 Circuit Breaker Logic ([`undo_guardrail.py`](../standalone_agent/undo_guardrail.py))
- **Execution Tracking:** Every executed device action is recorded in `UndoRateGuardrail`.
- **Undo Tracking:** Every user reversal via `undo_token` increments the undo counter.
- **Threshold Evaluation:** When sample size $\ge 5$ executions:
  $$\text{Undo Rate} = \frac{\text{Total Undos}}{\text{Total Executions}}$$
- **Automatic Pause:** If $\text{Undo Rate} > 0.05$ (5%), the circuit breaker **automatically trips**.
- **User Protection:** When tripped, subsequent focus proposals for that action are downgraded from automatic execution (`executed`) to explicit confirmation (`ask_user_confirmation`), notifying the user:
  *"Auto-execution paused by safety guardrail (Undo rate X% > 5%). Manual confirmation required."*
- **Reset Mechanism:** Users or administrators can inspect telemetry in the Control Plane UI and acknowledge/reset the circuit breaker via `POST /api/v1/safety/guardrails/reset`.

---

## 6. STRIDE Threat Model & Mitigations

| Threat Category | Potential Attack Vector | AURA Architectural Mitigation |
|---|---|---|
| **Spoofing** | Attacker impersonates a user's Fire TV or Echo to issue false state or spoof commands. | mTLS WebSocket ingress with short-lived JWTs. Target devices require Amazon Secrets Manager ARN binding (`arn:aws:secretsmanager:...`); device auth is validated before dispatch. |
| **Tampering** | In-transit tampering of device commands or replay of pause directives. | Idempotency keys (`idempotency_key`) uniquely bound to `{user_id}:{session_id}:{timestamp}_{nonce}`. Replayed requests return cached receipts without re-execution. |
| **Repudiation** | User or device claims an unauthorized intervention occurred without record. | Immutable Action Audit Trail logged to single-table DynamoDB with KMS customer-managed encryption. Every action logs `audit_id`, `undo_token`, `timestamp`, and `status`. |
| **Information Disclosure** | Connected devices leak private conversation transcripts or media titles to third parties. | **Zero Raw Media Boundary:** Adapters receive only minimized commands (`request_pause`, `request_dnd`). No transcript, meeting summary, topic, or calendar details are sent to device adapters. |
| **Denial of Service** | Malicious script rapidly flaps device commands, crashing TV apps or degrading network. | Per-device sliding-window rate limiting (maximum 5–10 commands per 60 seconds). Excess requests fail safe with `rate_limit_exceeded`. |
| **Elevation of Privilege** | An unprivileged user triggers pause on devices in an adjacent apartment or conference room. | Household Boundary Isolation: `household_id` validation blocks cross-household dispatch (`blocked_household_mismatch`). Allowlist check enforces user ownership. |

---

## 7. Verification & Automated Test Coverage

The Phase 5 security and certification guarantees are verified by automated test suites in continuous integration:

- [`tests/unit/test_adapter_certification.py`](../tests/unit/test_adapter_certification.py):
  - Certified device metadata and Secrets Manager ARN binding validation.
  - Household boundary mismatch blocking tests.
  - Sliding-window command rate limiting enforcement tests.
- [`tests/unit/test_feature_flags.py`](../tests/unit/test_feature_flags.py):
  - Global, Tenant, and User tier resolution.
  - Fail-closed kill switch validation.
  - Deterministic cohort rollout tests.
- [`tests/unit/test_undo_rate_guardrail.py`](../tests/unit/test_undo_rate_guardrail.py):
  - Precision tracking of execution vs. undo ratios.
  - >5% threshold automatic circuit breaker trip.
  - Minimum sample size protection against false positives.
  - Circuit breaker reset workflow.
- [`tests/e2e/test_connected_devices_e2e.py`](../tests/e2e/test_connected_devices_e2e.py):
  - End-to-end integration test validating the entire Phase 5 lifecycle across API Gateway, Lambda, Policy Engine, Feature Flags, Device Adapter, and Audit Trail.

---

## 8. General Availability (GA) Gate Sign-Off Checklist

- [x] **Documented Control Paths Only:** Certified Fire TV MediaSession transport controls and Echo DND verified.
- [x] **No Unverified Remote Control:** Arbitrary ADB, shell commands, or uncertified hardware calls strictly prohibited.
- [x] **Per-Capability Consent:** Devices remain idle until explicit `ConsentGrant` is recorded.
- [x] **3-Tier Kill Switches:** Instant Global, Tenant, and User isolation implemented and tested.
- [x] **Safety Circuit Breaker:** PRD Section 6 >5% undo rate automatic pause active.
- [x] **Household Boundary Isolation:** Cross-household directives blocked.
- [x] **Rate Limiting:** Sliding-window command throttling enforced per device.
- [x] **Credential Hygiene:** AWS Secrets Manager ARN references required; zero plaintext tokens.
- [x] **Zero Raw Transcripts:** Audit trail and device payloads contain zero raw text or media streams.
- [x] **Automated Test Suite:** 100% test pass rate across 131 phase-specific tests (and 517 total across the complete repository test suite).

---

**Author / Architect:** Aditya Shinde  
*Project AURA · All Rights Reserved*
