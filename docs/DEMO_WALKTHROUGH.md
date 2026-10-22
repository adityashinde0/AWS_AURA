# Project AURA — Demo Walkthrough & Portfolio Evaluation Guide

> **Live Deployment:** [https://awsaura-frontend.vercel.app/](https://awsaura-frontend.vercel.app/)
> **Repository:** [https://github.com/adityashinde0/AWS_AURA](https://github.com/adityashinde0/AWS_AURA)
> **Official Teaser:** [https://youtu.be/TGYBgcJrPzw](https://youtu.be/TGYBgcJrPzw)

---

## 🎯 Purpose of This Guide

This document provides a structured, reproducible walkthrough for technical evaluators, portfolio reviewers, and system architects. It demonstrates Project AURA’s end-to-end functionality across both the live web suite and local CLI/test environments, explicitly highlighting:

1. The primary user journeys (Pre-Meeting Intelligence and Focus Guardian).
2. The deterministic safety and anti-hallucination boundaries.
3. System behavior during failure modes (missing evidence, model hallucination, service timeouts).
4. What is running as real code, what is simulated in the browser, and what requires live cloud credentials.

---

## ⏱️ Evaluation Options

| Tier | Duration | Surfaces Covered | Best For |
|---|---|---|---|
| **Tier 1: 30-Second Glance** | ~30 seconds | [Live Landing Showcase](https://awsaura-frontend.vercel.app/) & [Official Video](https://youtu.be/TGYBgcJrPzw) | Quick portfolio overview, architecture diagram, design language |
| **Tier 2: 5-Minute Interactive Lab** | ~5 minutes | [Multi-Device Simulator](https://awsaura-frontend.vercel.app/simulator) & [Web Control Plane](https://awsaura-frontend.vercel.app/control-plane) | Interactive scenario testing, watch emulation, undo tokens, audit trail |
| **Tier 3: Technical Deep Dive** | ~15 minutes | CLI test suite (`py -m pytest`), contract schemas, and offline demo | Code verification, contract assertions, deterministic policy enforcement |

---

## 🚀 Tier 1: The 30-Second Glance (Landing Showcase)

1. Open [**Project AURA Showcase**](https://awsaura-frontend.vercel.app/).
2. **Observe the Hero:** Note the core value proposition: *Anticipates what you are about to forget at $T-5$ minutes, and orchestrates your physical and digital living spaces to defend your cognitive focus.*
3. **Play the Official Teaser:** Click the video player in the hero section to see the industrial design concept and calm technology interaction model.
4. **Inspect the Architecture:** Scroll to the **AWS Cloud Architecture** section to examine the 11-service serverless pipeline and dual-LLM resilience gateway.
5. **Toggle Display Theme:** Click the theme toggle pill in the top navigation to verify seamless dark/light mode switching with zero flash of unstyled content (FOUC).

---

## 🔬 Tier 2: The 5-Minute Interactive Lab (Multi-Device Simulator & Control Plane)

### Surface A: Multi-Device Hardware Simulation Lab
**URL:** [https://awsaura-frontend.vercel.app/simulator](https://awsaura-frontend.vercel.app/simulator)

The simulator emulates two distinct wearable platforms side-by-side with a live cloud telemetry inspector and automated assertion runner.

#### Walkthrough Scenario 1: $T-5$ Pre-Meeting Briefing Card
1. In the top toolbar, select **AURA Orbit Watch** (Hardware MIP display).
2. Select **Scenario 1: T-5 Meeting Prep**.
3. **Inspect the Watch Screen:**
   - Notice the concise briefing summary: *"Architecture review with Sarah Chen."*
   - Observe the **3 Prep Questions** grounded in past project context.
   - Note the **Tone Label** (*Collaborative / High stakes*) and **Confidence Score** (*94%*).
4. **Physical Interaction:**
   - Click the tactile **Crescent Control Key** on the right side of the watch bezel, or click the **Mark as Reviewed** button on the screen.
   - Notice the amber ring pulse and the immediate update in the live telemetry wire log.
5. **Verify Telemetry:**
   - Look at the right-hand **Cloud Telemetry Wire Log**. Notice the EventBridge event `meeting.prep.requested` and Lambda response with verified evidence IDs (`ev_arch_01`, `ev_arch_02`).

#### Walkthrough Scenario 2: Starting a Deep-Work Focus Sprint
1. Click **Scenario 2: Start Focus**.
2. Switch client target to **Apple Watch** (watchOS 10 Retina OLED).
3. **Inspect the Watch Screen:**
   - Active focus timer starts (45 minutes).
   - Biometric heart rate indicator is displayed.
   - Notice the ambient device indicators: Fire TV paused, Hue lighting set to Amber 2700K, Echo DND active.
4. **Single-Use Undo Token:**
   - A single-use **Undo Token** is issued (`undo_tok_focus_...`).
   - Click the Apple Watch **Side Button** or tap **Undo Focus** to restore all devices to their previous state in one tap.

#### Walkthrough Scenario 3: "I'm Distracted" Ambient Nudge
1. Click **Scenario 3: I'm Distracted**.
2. **Inspect the Trigger:** A user-declared distraction signal (`focus.signal` with `signal_type: explicit_user_distracted`) is submitted.
3. **Observe the Policy Engine Decision:**
   - The deterministic policy engine evaluates the user’s consent grants.
   - It triggers a gentle haptic nudge, dims lighting to amber, and pauses background video playback.
   - In the right-hand panel, inspect the **State Machine Assertions** card confirming `policy_required: true`.

#### Walkthrough Scenario 4: Safety Check-in with 60-Second Cancellation Window
1. Click **Scenario 4: Safety Check-in**.
2. **Observe Acute Biometric Event:** The system simulates elevated cognitive or physiological strain.
3. **The 60-Second Grace Window:**
   - A prominent 60-second visual countdown starts on the watch screen with warning haptics.
   - **Click "I'm OK — Cancel Alert":**
     - The incident transitions to `cancelled`.
     - Notice in the telemetry log: `contacts_notified: false`. No emergency alert is dispatched.
   - *(Optional)* Let the timer expire or trigger manual SOS to observe simulated AWS SNS dispatch to emergency contacts.

---

### Surface B: Executive Web Control Plane
**URL:** [https://awsaura-frontend.vercel.app/control-plane](https://awsaura-frontend.vercel.app/control-plane)

The Web Control Plane provides full executive oversight over active focus sessions, pre-meeting intelligence, device registries, granular permissions, and audit logs.

#### Step-by-Step Exploration:
1. **Home Tab:**
   - View the next upcoming meeting card (*"14:00 — Core Cloud Architecture Sync"*).
   - Review live focus controls and the ambient device matrix.
2. **Status Tab (Explainability & Grounding):**
   - Click the **Status** tab in the sidebar.
   - Inspect the **Pre-Meeting Intelligence** card.
   - Click **"Why am I seeing this?"** to inspect the exact citations (`ev_01`, `ev_02`) that justified the briefing content.
3. **Products Tab (Connected Devices & Slack Center):**
   - Click the **Products** tab.
   - Review registered devices (Fire TV, Echo Dot, Philips Hue, Ecobee Smart AC).
   - Inspect the **Slack Status Center**: notice the zero-leakage preview showing *"In Focus until 14:45"*, with private context, transcripts, and scores strictly blocked from export.
4. **Settings Tab (Granular Permission Grants):**
   - Click **Settings**.
   - Review per-action consent controls with 4 distinct tiers: **Ask every time**, **Allow this session**, **Always**, and **Never**.
   - Notice the data deletion and integration revocation controls.
5. **Audit Trail Tab (Immutable Rollback Log):**
   - Click **Audit Trail**.
   - Review chronological records detailing timestamp, actor, policy evaluated, action taken, and Undo token outcome.
   - If an active undo token is present, click **Undo** to trigger state restoration.

---

## 🛡️ Edge Cases & Failure Mode Demonstrations

One of Project AURA’s primary architectural achievements is its deterministic resilience when external systems, models, or data fail. Here is how each failure mode is handled:

### 1. Missing Evidence / Insufficient Context
- **Condition:** An upcoming calendar event has no associated notes, commitments, or prior interaction threads in local memory.
- **Model / System Behavior:** Rather than hallucinating a meeting agenda or guessing attendee discussion points, the system triggers `safe_fallback(fallback_reason="insufficient_context")`.
- **User Experience:** The user receives a neutral card: *"No prior notes found for this meeting. Focus Guardian will monitor your deep work session if started."* No fabricated questions are presented.

### 2. Evidence Hallucination Guardrail Triggered
- **Condition:** An LLM generates a briefing question citing an evidence ID (e.g. `ev_fabricated_999`) that does not exist in the input request payload.
- **System Defense ([`lambda/focus_guardian.py#L376-L386`](../lambda/focus_guardian.py)):**
  ```python
  if not set(proposal.evidence_ids).issubset(set(valid_evidence_ids)):
      _log("WARNING", "evidence_hallucination_detected",
           fabricated_ids=list(set(proposal.evidence_ids) - set(valid_evidence_ids)))
      return safe_fallback(request, fallback_reason="hallucinated_evidence")
  ```
- **Outcome:** The model proposal is immediately discarded. A fail-closed deterministic fallback is returned. The incident is logged to CloudWatch with `fallback_reason: "hallucinated_evidence"`.

### 3. Primary LLM Outage / Timeout (Multi-LLM Failover)
- **Condition:** Amazon Bedrock Claude 3.5 Sonnet experiences a 5xx error, network timeout, or rate limiting (HTTP 429).
- **System Defense ([`lambda/resilience_gateway.py`](../lambda/resilience_gateway.py)):**
  1. The primary provider attempt reaches its enforced wall-clock timeout (8 seconds).
  2. The circuit breaker trips and logs `resilience_gateway_failover` with `correlation_id`.
  3. Execution immediately routes to **IBM Watsonx (Granite 3.0 8B Instruct)**.
  4. If Watsonx also fails, the system executes a deterministic local heuristic briefing. Total execution budget stays safely within Lambda's 30-second window.

### 4. Duplicate or Expired Undo Token
- **Condition:** A user double-taps an undo button, or clicks Undo after the 5-minute TTL has expired.
- **System Defense ([`lambda/control_plane.py`](../lambda/control_plane.py)):**
  - **Duplicate Tap:** DynamoDB atomic lease check recognizes the action is already `SUCCEEDED` and returns an idempotent `200 OK` with `already_undone: true`.
  - **Expired Token:** If the token has surpassed its TTL, the handler returns `410 Gone` with `error: "undo_token_expired"`.

---

## 💻 Tier 3: Technical Deep Dive & Local Verification

Evaluators can verify the entire test suite and inspect contracts on any machine with Python 3.11+:

### 1. Run the Complete Automated Test Suite (517 Tests)
```bash
# Clone the repository
git clone https://github.com/adityashinde0/AWS_AURA.git
cd AWS_AURA

# Run all 517 passing unit, contract, and e2e tests
py -m pytest
```
*Expected Output:* `517 passed in ~10.7s (100% pass rate)`

### 2. Run the Offline / Non-AWS-SDK Test Suite (508 Tests)
In environments where `boto3` is not installed, run with the 3 collection ignores:
```bash
py -m pytest --ignore=tests/e2e/test_pre_meeting_e2e.py \
             --ignore=tests/unit/test_bedrock_adapter.py \
             --ignore=tests/unit/test_calendar_adapter.py
```
*Expected Output:* `508 passed in ~12.4s (100% pass rate)`

### 3. Run the Standalone Interactive Live Demo
```bash
# Executes the terminal-based interactive simulation of the companion
python -m standalone_agent.live_demo --auto
```

### 4. Serve the Web Platform Locally
```bash
npx serve frontend -p 3000
# Open http://localhost:3000 in your browser
```

---

## 📊 Implementation Reality Summary

For complete clarity during technical evaluation:

| Dimension | Real Code in Repo | Simulated in Web Suite | Requires Live AWS Setup |
|---|---|---|---|
| **$T-5$ Briefing Synthesis** | ✅ Pydantic models, JSON schema, citation checks | ✅ Dynamic simulation in `app.js` and `simulator.js` | Bedrock / Watsonx API keys |
| **Anti-Hallucination Guard** | ✅ Mathematical subset check in `focus_guardian.py` | ✅ Asserted in browser simulator test cockpit | None (runs in Lambda) |
| **Deterministic Policy Engine** | ✅ Granular permission gate in `policy_engine.py` | ✅ Enforced in web control plane | None (runs locally / edge) |
| **Cognito RS256 Auth Gate** | ✅ Real RSA verification in `lambda/auth.py` | ✅ Validated via client Bearer token headers | Cognito User Pool ID |
| **Undo State Machine** | ✅ Fencing tokens & durable DynamoDB transitions | ✅ Interactive 1-tap undo widgets | DynamoDB Table |
| **Physical Hardware Actuation** | ✅ Zephyr RTOS firmware & adapter contracts | ✅ Simulated Fire TV, Echo, and Hue states | Physical smart home devices |
| **Orbit Hardware Pebble** | ✅ Zephyr C code & complete engineering dossier PDF | ✅ Interactive MIP canvas emulation & amber ring | Physical custom hardware PCB |
| **Apple Watch Companion** | ✅ Native SwiftUI & HealthKit code in `apple/` | ✅ Interactive watchOS 10 OLED emulation | Apple Developer Program |

---

*Project AURA — Proactive Memory That Fights For Your Focus*
