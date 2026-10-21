# AURA Web Suite & Client Architecture

> **Live Production Deployment:** [https://awsaura-frontend.vercel.app/](https://awsaura-frontend.vercel.app/)<br/>
> **Framework:** Zero-dependency modern vanilla web stack (HTML5, Vanilla CSS, ES6 JavaScript)<br/>
> **Hosting & Edge Delivery:** Vercel (Edge Network with immutable asset caching and security headers)

---

## 🌐 Live Deployment Surfaces

| Surface | Direct URL | Source Files | Description |
|---|---|---|---|
| **Landing Showcase** | [`/`](https://awsaura-frontend.vercel.app/) | [`index.html`](index.html), [`landing.css`](landing.css), [`landing.js`](landing.js) | Public-facing landing showcase featuring embedded teaser video, architectural pillars, multi-device ecosystem overview, and responsive navigation. |
| **Multi-Device Simulator** | [`/simulator`](https://awsaura-frontend.vercel.app/simulator) | [`simulator.html`](simulator.html), [`simulator.css`](simulator.css), [`simulator.js`](simulator.js) | Interactive browser lab emulating the **AURA Orbit Hardware Pebble** and **Apple Watch Series 9** with 4 live scenarios, haptic states, and real-time wire logs. |
| **Executive Control Plane** | [`/control-plane`](https://awsaura-frontend.vercel.app/control-plane) | [`control-plane.html`](control-plane.html), [`index.css`](index.css), [`app.js`](app.js) | Production executive dashboard for meeting briefings, focus sessions, device allowlists, granular permissions, and immutable audit logs. |
| **Orbit 360° Explorer** | [`/orbit`](https://awsaura-frontend.vercel.app/orbit) | [`orbit.html`](orbit.html), [`orbit.css`](orbit.css), [`orbit.js`](orbit.js) | Hardware deep-dive featuring 3D exploded view, PMIC nPM1300 power budget, Nordic nRF5340 dual-core specifications, and BOM analysis. |

---

## 📐 Client Architecture & Dual Runtime Modes

The web client controller ([`frontend/app.js`](app.js)) implements an adaptive **Dual-Mode Architecture** configured via [`API_CONFIG`](app.js#L11):

```
┌────────────────────────────────────────────────────────────────────────┐
│                      AURA WEB CLIENT CONTROLLER                        │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   Mode 1: Local Demo / Simulation Mode (Default)                       │
│   • Trigger: No backend API URL provided                               │
│   • Behavior: Zero cloud dependencies; generates rich client-side       │
│     simulated events for meetings, focus timers, and smart devices.    │
│   • Target: Instant portfolio evaluation and offline presentations.    │
│                                                                        │
│   Mode 2: Live Connected Mode (Enterprise Edge)                        │
│   • Trigger: URL query param (?api=https://...) or localStorage        │
│   • Behavior: Dispatches authenticated REST calls to Amazon API         │
│     Gateway HTTP v2 with Cognito RS256 Bearer JWTs.                    │
│   • Safety Invariant: Truthful error reporting—never silently falls    │
│     back to fake demo data when connected endpoints return 4xx/5xx.     │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

### Supported API Endpoints (Connected Mode)
When connected to an active AWS API Gateway deployment, the client interfaces with these verified routes:
* `POST /api/v1/focus/signal` — Distraction signal ingress and evaluation proposal.
* `POST /api/v1/focus/session/start` & `stop` — Deep work session lifecycle management.
* `GET /api/v1/consent/grants` & `POST` — Granular capability authorization.
* `GET /api/v1/audit/records` — Immutable action audit log retrieval.
* `POST /api/v1/actions/undo` — Single-use cryptographic rollback execution.
* `POST /api/v1/safety/cancel` — 60-second personal safety escalation cancellation.

---

## 🎨 Design System & Theme Engine

1. **Vanilla CSS Architecture:**
   - All styles are implemented using pure, fluid CSS with custom design tokens (`--bg-primary`, `--accent-amber`, `--text-primary`, `--border-subtle`).
   - Strict avoidance of bloated CSS frameworks to ensure instant cold-start page loads and sub-50ms render latency.
2. **Flash-of-Unstyled-Content (FOUC) Prevention:**
   - Every HTML surface embeds an inline synchronous script in `<head>` that reads `localStorage.getItem('aura-color-theme')` and sets `data-theme` on `<html>` before DOM painting begins.
3. **Accessibility (WCAG AA Compliance):**
   - High-contrast text ratios across both light and dark themes.
   - Screen-reader labels (`aria-label`, `aria-expanded`, `aria-controls`, `role="tab"`, `role="toolbar"`).
   - Visible keyboard focus rings (`:focus-visible`).
   - No color-only status indicators: all alerts and badges pair color with descriptive textual labels or iconography.

---

## 📋 AURA Control Plane — UX Contract

The control plane implements these non-negotiable design invariants:

- **Today:** Next meeting, source evidence labels, brief usefulness feedback, and “Why am I seeing this?” explainability drawer.
- **Focus:** Start/stop, selected duration, enabled signals, score explanation, suggested action, and immediate Undo.
- **Permissions:** Per-source / per-action grants (**Ask every time**, **This session**, **Always**, **Never**); clear revoke and export/delete path.
- **Integrations:** Connection health, exact Slack channel, device allowlist, and adapter capability/limitations.
- **Audit:** Immutable action timeline with actor, policy, reason, result, and undo outcome; never displays raw private transcripts by default.
- **Anti-Surveillance Rule:** Strictly zero employee leaderboards, manager productivity monitoring, or covert attentiveness tracking.

---

## 🧪 Local Development & Review

To run and review the web suite locally without any external build tooling:

```bash
# From the repository root:
npx serve frontend -p 3000

# Open http://localhost:3000 in your browser
```

To test the connected mode with a mock or live backend endpoint:
```
http://localhost:3000/control-plane.html?api=http://localhost:8000
```
