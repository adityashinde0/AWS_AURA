/**
 * Project AURA — Mobile-First Control Plane & Focus Guardian
 * Frontend Client Controller
 */

// Application State
// ==========================================================================
// Runtime Backend API Configuration & Integration Client
// ==========================================================================

const API_CONFIG = {
  getBaseUrl() {
    // 1. URL query parameters (?api=... or ?backend=...)
    if (typeof window !== 'undefined' && window.location && window.location.search) {
      const params = new URLSearchParams(window.location.search);
      const queryApi = params.get('api') || params.get('backend');
      if (queryApi && queryApi.trim()) {
        return queryApi.trim().replace(/\/+$/, '');
      }
    }
    // 2. Window runtime global
    if (typeof window !== 'undefined' && window.AURA_API_ENDPOINT && typeof window.AURA_API_ENDPOINT === 'string') {
      return window.AURA_API_ENDPOINT.trim().replace(/\/+$/, '');
    }
    // 3. LocalStorage persistence
    if (typeof window !== 'undefined' && window.localStorage) {
      const stored = window.localStorage.getItem('aura_api_base_url');
      if (stored && stored.trim()) {
        return stored.trim().replace(/\/+$/, '');
      }
    }
    // Default: Live deployed AWS API Gateway endpoint
    return 'https://trujdwjp17.execute-api.us-east-1.amazonaws.com';
  },

  setBaseUrl(url) {
    if (typeof window !== 'undefined' && window.localStorage) {
      if (url && url.trim()) {
        window.localStorage.setItem('aura_api_base_url', url.trim().replace(/\/+$/, ''));
      } else {
        window.localStorage.removeItem('aura_api_base_url');
      }
    }
  },

  isConnected() {
    return Boolean(this.getBaseUrl());
  },

  timeoutMs: 5000,
};

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

const authSession = {
  _token: null,
  getToken() {
    if (this._token) return this._token;
    if (typeof window !== 'undefined' && window.__AURA_AUTH_TOKEN__) {
      return window.__AURA_AUTH_TOKEN__;
    }
    if (typeof window !== 'undefined' && window.sessionStorage) {
      return window.sessionStorage.getItem('aura_auth_token') || null;
    }
    return null;
  },
  setToken(token) {
    this._token = token;
    if (typeof window !== 'undefined' && window.sessionStorage) {
      if (token) {
        window.sessionStorage.setItem('aura_auth_token', token);
      } else {
        window.sessionStorage.removeItem('aura_auth_token');
      }
    }
  },
  clearToken() {
    this.setToken(null);
  },
};

const auraApiClient = {
  authSession,

  async _request(path, method = 'GET', body = null) {
    const baseUrl = API_CONFIG.getBaseUrl();
    if (!baseUrl) {
      return { success: false, mode: 'demo', error: 'demo_mode', message: 'Running in Local Demo Mode' };
    }

    const url = `${baseUrl}${path}`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), API_CONFIG.timeoutMs);

    const options = {
      method,
      headers: {
        'Accept': 'application/json',
      },
      signal: controller.signal,
    };

    const authToken = authSession.getToken();
    if (authToken) {
      options.headers['Authorization'] = `Bearer ${authToken}`;
    }

    if (body && (method === 'POST' || method === 'PUT' || method === 'PATCH')) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }

    try {
      const response = await fetch(url, options);
      clearTimeout(timeoutId);

      const contentType = response.headers.get('content-type') || '';
      let data = null;
      if (contentType.includes('application/json')) {
        data = await response.json();
      } else {
        const text = await response.text();
        try {
          data = JSON.parse(text);
        } catch (_) {
          data = { raw: text };
        }
      }

      if (!response.ok) {
        let errType = data?.error || 'http_error';
        if (response.status === 401) errType = 'unauthorized';
        else if (response.status === 403) errType = 'forbidden';
        else if (response.status === 404) errType = 'not_found';
        else if (response.status === 409) errType = 'conflict';
        else if (response.status === 410) errType = 'expired';
        else if (response.status === 429) errType = 'throttled';
        else if (response.status >= 500) errType = 'server_error';

        return {
          success: false,
          mode: 'connected',
          status: response.status,
          error: errType,
          message: data?.message || `HTTP ${response.status}`,
          data,
        };
      }

      return {
        success: true,
        mode: 'connected',
        status: response.status,
        data,
      };
    } catch (err) {
      clearTimeout(timeoutId);
      const isAbort = err.name === 'AbortError';
      return {
        success: false,
        mode: 'connected',
        error: isAbort ? 'timeout' : 'network_error',
        message: isAbort ? `Request timed out after ${API_CONFIG.timeoutMs}ms` : (err.message || 'Network request failed'),
      };
    }
  },

  postFocusSignal(payload) {
    return this._request('/api/v1/focus/signal', 'POST', payload);
  },

  startSession(durationMinutes, sessionId) {
    return this._request('/api/v1/focus/session', 'POST', {
      user_id: state.userId,
      action: 'start',
      duration_minutes: durationMinutes,
      session_id: sessionId,
    });
  },

  stopSession(sessionId) {
    return this._request('/api/v1/focus/session', 'POST', {
      user_id: state.userId,
      action: 'stop',
      session_id: sessionId,
    });
  },

  getGrants() {
    return this._request(`/api/v1/consent/grants?user_id=${encodeURIComponent(state.userId)}`, 'GET');
  },

  createGrant(grant) {
    return this._request('/api/v1/consent/grants', 'POST', {
      user_id: state.userId,
      action_type: grant.action_type,
      scope: grant.scope,
      target_allowlist: grant.target_allowlist || [],
      ttl_seconds: grant.ttl_seconds || 86400,
    });
  },

  revokeGrant(grantId) {
    return this._request(`/api/v1/consent/grants/${encodeURIComponent(grantId)}?user_id=${encodeURIComponent(state.userId)}`, 'DELETE');
  },

  getAuditRecords(limit = 20) {
    return this._request(`/api/v1/audit/actions?user_id=${encodeURIComponent(state.userId)}&limit=${limit}`, 'GET');
  },

  undoAction(undoToken) {
    return this._request('/api/v1/actions/undo', 'POST', {
      user_id: state.userId,
      undo_token: undoToken,
    });
  },
};

function ensureBackendStyles() {
  if (document.getElementById('aura-backend-styles')) return;
  const style = document.createElement('style');
  style.id = 'aura-backend-styles';
  style.textContent = `
    .backend-mode-pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      font-size: 0.78rem;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 9999px;
      cursor: pointer;
      user-select: none;
      transition: all 0.2s ease;
      border: 1px solid var(--border-color, rgba(255,255,255,0.14));
      background: var(--card-bg, rgba(255,255,255,0.06));
      color: var(--text-color, inherit);
      margin-right: 8px;
    }
    .backend-mode-pill:hover {
      border-color: rgba(245, 158, 11, 0.5);
      background: rgba(245, 158, 11, 0.1);
    }
    .backend-mode-pill.connected {
      border-color: rgba(16, 185, 129, 0.4);
      background: rgba(16, 185, 129, 0.08);
      color: #10B981;
    }
    .backend-mode-pill .mode-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      display: inline-block;
    }
    .backend-mode-pill .mode-dot.online {
      background: #10B981;
      box-shadow: 0 0 8px #10B981;
    }
    .backend-mode-pill .mode-dot.demo {
      background: #F59E0B;
      box-shadow: 0 0 8px #F59E0B;
    }
  `;
  document.head.appendChild(style);
}

function renderBackendIndicator() {
  ensureBackendStyles();
  let indicator = document.getElementById('aura-backend-indicator');
  const headerRight = document.querySelector('.header-right');
  if (!headerRight) return;

  if (!indicator) {
    indicator = document.createElement('div');
    indicator.id = 'aura-backend-indicator';
    indicator.className = 'backend-mode-pill';
    indicator.setAttribute('role', 'button');
    indicator.setAttribute('tabindex', '0');
    headerRight.insertBefore(indicator, headerRight.firstChild);

    indicator.addEventListener('click', promptBackendEndpoint);
    indicator.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        promptBackendEndpoint();
      }
    });
  }

  const isConnected = API_CONFIG.isConnected();
  const baseUrl = API_CONFIG.getBaseUrl();
  if (isConnected) {
    let host = baseUrl;
    try {
      const u = new URL(baseUrl);
      host = u.host;
    } catch (_) {}
    indicator.className = 'backend-mode-pill connected';
    indicator.innerHTML = `
      <span class="mode-dot online" aria-hidden="true"></span>
      <span class="mode-label">Backend: <strong>${escapeHtml(host)}</strong></span>
    `;
    indicator.title = `Connected to ${baseUrl}. Click to change or switch to Demo Mode.`;
  } else {
    indicator.className = 'backend-mode-pill demo';
    indicator.innerHTML = `
      <span class="mode-dot demo" aria-hidden="true"></span>
      <span class="mode-label">Demo Mode (Simulated)</span>
    `;
    indicator.title = 'Running in Local Demo / Simulation Mode. Click to configure live backend API URL.';
  }
}

function promptBackendEndpoint() {
  const current = API_CONFIG.getBaseUrl();
  const input = prompt(
    'Configure AURA Backend API Endpoint:\\n\\nEnter the base URL of your API (e.g., https://xyz.execute-api.us-east-1.amazonaws.com or http://localhost:8000).\\nLeave blank to run in Local Demo / Simulation Mode.',
    current
  );
  if (input !== null) {
    const trimmed = input.trim();
    API_CONFIG.setBaseUrl(trimmed);
    renderBackendIndicator();
    if (trimmed) {
      showToast(`🔗 Backend set to: ${trimmed}. Testing connection...`);
      syncWithBackend();
    } else {
      showToast('ℹ️ Switched to Local Demo / Simulation Mode.');
    }
  }
}

async function syncWithBackend() {
  if (!API_CONFIG.isConnected()) return;
  try {
    const [grantsResp, auditResp] = await Promise.all([
      auraApiClient.getGrants(),
      auraApiClient.getAuditRecords(20),
    ]);

    if (grantsResp.success && Array.isArray(grantsResp.data?.grants) && grantsResp.data.grants.length > 0) {
      state.grants = grantsResp.data.grants;
      renderGrants();
    }

    if (auditResp.success && Array.isArray(auditResp.data?.records) && auditResp.data.records.length > 0) {
      const mapped = auditResp.data.records.map((r) => ({
        action_id: r.action_id,
        timestamp: r.timestamp < 1e11 ? r.timestamp * 1000 : r.timestamp,
        action_type: r.action_type,
        target: r.target_device_id || 'Living Room Fire TV',
        status: r.status,
        reason: r.reason,
        undo_token: r.undo_token || null,
      }));
      state.audit = mapped;
      renderAuditTimeline();
    }

    if (grantsResp.success || auditResp.success) {
      showToast('✅ Synchronized state with live backend.');
    } else {
      showToast(`⚠️ Backend unreachable (${grantsResp.message || auditResp.message}). Operating in demo fallback.`, false);
    }
  } catch (err) {
    showToast(`⚠️ Backend sync error: ${err.message}. Operating in demo fallback.`, false);
  }
}

function updateTvUI(status) {
  const isPaused = status === 'paused';
  if (DOM.firetvBadge) {
    DOM.firetvBadge.textContent = isPaused ? 'Paused' : 'Playing';
    DOM.firetvBadge.className = isPaused ? 'badge badge-status' : 'badge badge-success';
  }
  if (DOM.actionStatusBadge) {
    DOM.actionStatusBadge.textContent = isPaused ? 'Executed (Paused)' : 'Restored (Playing)';
    DOM.actionStatusBadge.className = isPaused ? 'badge badge-status' : 'badge badge-success';
  }
  const btnHomePauseTv = document.getElementById('btn-home-pause-tv');
  if (btnHomePauseTv) {
    btnHomePauseTv.textContent = isPaused ? '▶️ Resume Fire TV' : '📺 Pause Fire TV';
  }
  const tileTvInd = document.getElementById('tile-tv-indicator');
  if (tileTvInd) {
    tileTvInd.style.background = isPaused ? '#f59e0b' : '#10B981';
  }
}

const state = {
  userId: 'usr_marcus',
  activeMeetingId: 'q4_arch',
  whisperAudioPlaying: false,
  whisperTimer: null,
  whisperSeconds: 14,
  session: {
    active: true,
    sessionId: 'sess_marcus_' + Math.random().toString(36).substring(2, 9),
    durationMinutes: 25,
    remainingSeconds: 25 * 60,
    timerInterval: null,
    isDistracted: false,
    score: 0.0,
    reason: 'Focus session running uninterrupted. Score decays automatically over time.',
  },
  bank: {
    balance: 12,
    privateOnly: true,
  },
  activeUndoToken: null,
  grants: [
    {
      grant_id: 'grant_firetv_pause',
      action_type: 'request_pause',
      scope: 'allow_session',
      target_allowlist: ['dev_firetv_living_room'],
      active: true,
    },
    {
      grant_id: 'grant_echo_dnd',
      action_type: 'request_dnd',
      scope: 'ask_every_time',
      target_allowlist: ['dev_echo_office'],
      active: true,
    },
    {
      grant_id: 'grant_prep_brief',
      action_type: 'pre_meeting_briefing',
      scope: 'always',
      target_allowlist: [],
      active: true,
    },
    {
      grant_id: 'grant_slack_status',
      action_type: 'slack_status',
      scope: 'allow_session',
      target_allowlist: ['#marcus-focus'],
      active: true,
    },
  ],
  learnedRoutines: [
    {
      routine_id: 'rtn_wind_down',
      intent_pattern: 'I am tired, want to sleep',
      name: 'Evening Wind-Down Routine',
      scope: 'always',
      active: true,
      actions: [
        { device: 'dev_firetv_living_room', action: 'request_pause', description: 'Pause video playback' },
        { device: 'dev_smart_lights', action: 'dim_lights', description: 'Dim lighting to 20%' },
      ],
    },
    {
      routine_id: 'rtn_quick_break',
      intent_pattern: 'Need a quick break',
      name: '15-min Breather Routine',
      scope: 'always',
      active: true,
      actions: [
        { device: 'dev_firetv_living_room', action: 'request_pause', description: 'Pause active screens' },
        { device: 'dev_echo_office', action: 'request_dnd', description: '15-min Breather status' },
      ],
    },
  ],
  featureFlags: {
    global_switches: {
      request_pause: true,
      request_dnd: true,
      watch_whisper: true,
      slack_post: true,
    },
    rollout_percentage: 100,
  },
  guardrail: {
    executions: 12,
    undos: 0,
    undoRate: 0.0,
    threshold: 0.05,
    autoExecutionPaused: false,
  },
  connectedDevices: [
    {
      id: 'dev_firetv_living_room',
      name: 'Living Room Fire TV Stick 4K',
      type: 'fire_tv',
      standard: 'Amazon Fire TV MediaSession API v2 (Certified)',
      status: 'certified',
      household: 'household_default',
      rateLimit: '10/min',
      state: 'playing',
    },
    {
      id: 'dev_echo_office',
      name: 'Office Echo Dot 5th Gen',
      type: 'echo_speaker',
      standard: 'Alexa Smart Home DoNotDisturb v3 (Certified)',
      status: 'certified',
      household: 'household_default',
      rateLimit: '10/min',
      state: 'dnd_inactive',
    },
    {
      id: 'dev_echo_kitchen',
      name: 'Kitchen Echo Show 8',
      type: 'echo_speaker',
      standard: 'Alexa Smart Home DoNotDisturb v3 (Certified)',
      status: 'certified',
      household: 'household_default',
      rateLimit: '10/min',
      state: 'dnd_inactive',
    },
    {
      id: 'dev_firetv_bedroom_staged',
      name: 'Bedroom Fire TV Cube',
      type: 'fire_tv',
      standard: 'Amazon MediaSession v2 (Beta)',
      status: 'staged',
      household: 'household_default',
      rateLimit: '5/min',
      state: 'playing',
    },
    {
      id: 'dev_smart_tv_unsupported',
      name: 'Old Uncertified Smart TV',
      type: 'generic_smart_device',
      standard: 'Legacy Uncertified Protocol',
      status: 'uncertified',
      household: 'household_default',
      rateLimit: '3/min',
      state: 'idle',
    },
  ],
  latestReceipt: null,
  slack: {
    connected: true,
    workspaceId: 'T012345678',
    workspaceName: 'Marcus Enterprise HQ',
    adminApproved: true,
    allowedChannels: ['#marcus-focus', '#deep-work-ambient', 'user_profile'],
    selectedChannel: '#marcus-focus',
    durationMinutes: 45,
    activeReceipt: null,
    previewText: 'In focus until 15:00',
    previewEmoji: '🎯',
  },
  devices: {
    firetv: { id: 'dev_firetv_living_room', state: 'playing', supported: true },
    echo: { id: 'dev_echo_office', state: 'dnd_inactive', supported: true },
  },
  audit: [
    {
      action_id: 'act_init_sess',
      timestamp: Date.now() - 1000 * 60 * 12,
      action_type: 'focus_started',
      target: 'Local Companion',
      status: 'executed',
      reason: '25-minute Pomodoro focus session initialized by user',
      undo_token: null,
    },
    {
      action_id: 'act_briefing_t5',
      timestamp: Date.now() - 1000 * 60 * 8,
      action_type: 'pre_meeting_briefing',
      target: 'Bee Watch / Whisper Card',
      status: 'executed',
      reason: 'Delivered 52-word pre-meeting brief for Q4 Strategy Review (T-5 min trigger)',
      undo_token: null,
    },
  ],
};

// Meetings Mock Database
const meetingsData = {
  q4_arch: {
    time: "14:30",
    title: "Q4 Strategy & Architecture Review",
    participants: "Participants: Marcus, Aditi, Sarah Chen (3)",
    tone: "Tone: Confident",
    wordCount: 52,
    summary: "Team is reviewing Q4 cloud migration milestones. Key dependency is DynamoDB single-table latency and API Gateway WebSocket authorization. Aditi flagged open question regarding Fire TV adapter certification and rollback latency. Target outcome is sign-off on production architecture boundaries.",
    questions: [
      "What is the measured p95 latency for local companion WebSocket relay?",
      "Has the Fire TV MediaSession pause capability received user session consent?",
      "Are quiet hours configured to suppress briefings during evening deep work?"
    ],
    evidence: ["evt_mem_arch_01", "evt_cal_q4_sync", "evt_commit_cdk_review"]
  },
  bedrock_sync: {
    time: "16:00",
    title: "Bedrock Vector Migration & Guardrails",
    participants: "Participants: Marcus, Liam Zhao, Elena Rostova (3)",
    tone: "Tone: Analytical & Direct",
    wordCount: 48,
    summary: "Reviewing Amazon Bedrock Claude 3.5 Sonnet token budget and vector search thresholds in Pinecone/OpenSearch. Liam identified potential cache hit rate improvement using local semantic hashes. Goal: establish zero-retraining attestation for enterprise audits.",
    questions: [
      "What is the token truncation boundary on vector search grounding snippets?",
      "Can local hashing eliminate cloud calls for identical focus distraction signatures?",
      "Is the client-side redaction pipeline verified against GDPR Art. 17 right-to-erasure?"
    ],
    evidence: ["evt_bedrock_guardrail", "evt_vector_cache", "evt_gdpr_contract"]
  },
  firetv_ambient: {
    time: "17:15",
    title: "Fire TV & Ambient Defense Hardware",
    participants: "Participants: Marcus, Dave K., Priya Patel (3)",
    tone: "Tone: Collaborative & Decisive",
    wordCount: 45,
    summary: "Aligning on companion band haptic feedback patterns and Living Room Fire TV transport control. Validated 200ms pause response with reversible undo token. Dave agreed on zero audio upload policy for local sound classification.",
    questions: [
      "Does the Fire TV pause command issue an immediate undo notification on the band?",
      "How does the Alexa DND capability handle incoming emergency phone calls?",
      "What is the battery drain impact of continuous low-energy BLE advertisement?"
    ],
    evidence: ["evt_firetv_mediasession", "evt_ble_spec", "evt_echo_dnd"]
  }
};

// Evidence Forensic Database
const evidenceDatabase = {
  evt_mem_arch_01: {
    badge: "📌 Local Bee Memory",
    title: "Slack Architecture Sync Grounding",
    id: "evt_mem_arch_01",
    redaction: "✅ Local PII Sanitized (Presidio + Regex)",
    time: "Today, 11:20 UTC (Local RAM)",
    sha: "SHA-256: 7f8a92bb034e31b49129df4e1a0b38",
    snippet: `{\n  "source": "Bee Companion / Slack #architecture-sync",\n  "scrubbed_fields": ["[REDACTED_EMAIL]", "[REDACTED_AWS_KEY]"],\n  "captured_context": "Aditi noted Fire TV pause latency target is <200ms with reversible undo. DynamoDB single-table verified.",\n  "storage_policy": "EPHEMERAL_RAM_ONLY"\n}`
  },
  evt_cal_q4_sync: {
    badge: "📅 Calendar Invite",
    title: "Google Calendar / Exchange Ingress",
    id: "evt_cal_q4_sync",
    redaction: "✅ Local Attendees Scoped (3 consented)",
    time: "Today, 09:00 UTC",
    sha: "SHA-256: 4a2e19cc1789c88d89a20fe1128d54",
    snippet: `{\n  "source": "Calendar Service (T-5 evaluation)",\n  "event_title": "Q4 Strategy & Architecture Review",\n  "attendees": ["usr_marcus", "aditi@team", "sarah.c@cloud"],\n  "external_meeting_link": "[REDACTED_TOKEN_SCRUBBED]"\n}`
  },
  evt_commit_cdk_review: {
    badge: "📝 Commit Note",
    title: "Git Repository Construct Boundary",
    id: "evt_commit_cdk_review",
    redaction: "✅ Local Code Note Grounding",
    time: "Yesterday, 17:45 UTC",
    sha: "SHA-256: 91cb541a720bf012ea8810cd0491ab",
    snippet: `{\n  "repo": "aws-aura-companion",\n  "commit": "cdk-synth-v2-migration",\n  "summary": "Implement verified MediaSession pause/resume adapter with reversible idempotency tokens."\n}`
  },
  evt_bedrock_guardrail: {
    badge: "🛡️ Bedrock Guardrail",
    title: "AWS Bedrock Zero Data Retention Contract",
    id: "evt_bedrock_guardrail",
    redaction: "✅ Cryptographically Enforced",
    time: "Active Session",
    sha: "SHA-256: b208fe919a3d1264c1889b78912ef0",
    snippet: `{\n  "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",\n  "data_logging": "DISABLED",\n  "guardrails": "ENFORCED",\n  "privacy_audit": "PASSED"\n}`
  },
  evt_vector_cache: {
    badge: "⚡ Cache Config",
    title: "Local Ephemeral Semantic Hash Cache",
    id: "evt_vector_cache",
    redaction: "✅ Zero Cloud Transcripts",
    time: "Today, 12:00 UTC",
    sha: "SHA-256: 3c91af2209da3312bc8917e440182c",
    snippet: `{\n  "cache_type": "Local LRU",\n  "similarity_metric": "Cosine (threshold 0.88)",\n  "encryption": "AES-256-GCM (client-managed key)"\n}`
  },
  evt_gdpr_contract: {
    badge: "📜 Compliance Note",
    title: "GDPR Art 17 Cryptographic Purge Attestation",
    id: "evt_gdpr_contract",
    redaction: "✅ Verified User-Owned",
    time: "Audit Ready",
    sha: "SHA-256: 88fa3910c27de9123aa410bc39109d",
    snippet: `{\n  "right_to_erasure": "INSTANT_PURGE_ENABLED",\n  "cloud_retention": 0,\n  "employer_visibility": "STRICTLY_PROHIBITED"\n}`
  },
  evt_firetv_mediasession: {
    badge: "📺 Fire TV Spec",
    title: "MediaSession Certified Android Capability",
    id: "evt_firetv_mediasession",
    redaction: "✅ Capability-Scraped Grant",
    time: "Active Connection",
    sha: "SHA-256: 512da9f0291ca8789bc440ab48102d",
    snippet: `{\n  "target_device": "dev_firetv_living_room",\n  "action": "ACTION_PAUSE / ACTION_PLAY",\n  "reversible_undo_window_sec": 300\n}`
  },
  evt_ble_spec: {
    badge: "📡 BLE Spec",
    title: "Bluetooth Low Energy Wearable Profile",
    id: "evt_ble_spec",
    redaction: "✅ Local GATT Peripheral",
    time: "Paired (RSSI: -58dBm)",
    sha: "SHA-256: 19fa8230bbca10928fe8239014589d",
    snippet: `{\n  "peripheral": "Project AURA Smart Band",\n  "haptic_profile": "WHISPER_T5_PRE_MEETING",\n  "battery_level": "94%"\n}`
  },
  evt_echo_dnd: {
    badge: "🔊 Echo Alexa DND",
    title: "Alexa Smart Home Do Not Disturb Mode",
    id: "evt_echo_dnd",
    redaction: "✅ Authorized Token Scoped",
    time: "Standby",
    sha: "SHA-256: 77bc129038ea89a128df910948ab12",
    snippet: `{\n  "target_device": "dev_echo_office",\n  "action": "SET_DO_NOT_DISTURB",\n  "auto_restore": true\n}`
  }
};

// DOM Elements
const DOM = {
  // Navigation Tabs
  tabs: document.querySelectorAll('.nav-tab'),
  panels: document.querySelectorAll('.view-panel'),

  // Header & Theme
  themeToggleBtn: document.getElementById('theme-toggle-btn'),
  themeToggleLabel: document.getElementById('theme-toggle-label'),
  bankBadge: document.getElementById('header-bank-badge'),
  bankBalance: document.getElementById('bank-balance-display'),

  // Today Surface: Meeting Info
  meetingChips: document.querySelectorAll('.meeting-chip'),
  meetingTime: document.getElementById('meeting-time'),
  meetingTitle: document.getElementById('meeting-title'),
  meetingParticipants: document.getElementById('meeting-participants'),
  meetingToneBadge: document.getElementById('meeting-tone-badge'),
  briefingWordCount: document.getElementById('briefing-word-count'),
  briefingSummary: document.getElementById('briefing-summary'),
  briefingQuestionsList: document.getElementById('briefing-questions'),
  evidenceTagsContainer: document.getElementById('evidence-tags'),
  btnCopyBrief: document.getElementById('btn-copy-brief'),

  // Audio Whisper Briefing Player
  btnWhisperAudio: document.getElementById('btn-whisper-audio'),
  whisperBtnText: document.getElementById('whisper-btn-text'),
  whisperBtnIcon: document.getElementById('whisper-btn-icon'),
  audioWaveformPlayer: document.getElementById('audio-waveform-player'),
  waveformCountdown: document.getElementById('waveform-countdown'),
  waveformStatus: document.getElementById('waveform-status'),

  // T-5 Simulator
  btnSimulateT5: document.getElementById('btn-simulate-t5'),
  briefingTimerBadge: document.getElementById('briefing-timer-badge'),
  briefingTimerText: document.getElementById('briefing-timer-text'),

  // Today Briefing Feedback
  btnHelpful: document.getElementById('btn-feedback-helpful'),
  btnNotUseful: document.getElementById('btn-feedback-notuseful'),
  btnMuteSeries: document.getElementById('btn-feedback-mute'),

  // Focus Timer & Controls
  btnToggleFocus: document.getElementById('btn-toggle-focus'),
  timerClock: document.getElementById('timer-clock'),
  timerProgress: document.getElementById('timer-progress'),
  durationChips: document.querySelectorAll('.btn-chip'),
  focusStatusDot: document.getElementById('focus-dot'),
  focusStatusText: document.getElementById('focus-status-text'),

  // Distraction & Score
  btnReportDistracted: document.getElementById('btn-report-distracted'),
  scoreVal: document.getElementById('score-val'),
  scoreBar: document.getElementById('score-bar'),
  scoreReason: document.getElementById('score-reason'),

  // Action & Undo
  btnSimulateAction: document.getElementById('btn-simulate-action'),
  btnImmediateUndo: document.getElementById('btn-immediate-undo'),
  actionStatusBadge: document.getElementById('action-status-badge'),
  firetvBadge: document.getElementById('firetv-badge'),

  // Signals
  toggleSignalVideo: document.getElementById('toggle-signal-video'),
  toggleSignalApp: document.getElementById('toggle-signal-app'),

  // Modals
  btnWhySeeingThis: document.getElementById('btn-why-seeing-this'),
  modalWhy: document.getElementById('modal-why-seeing-this'),
  btnCloseModal: document.getElementById('btn-close-modal'),
  btnModalGotIt: document.getElementById('btn-modal-got-it'),

  // Evidence Inspector Modal
  modalEvidence: document.getElementById('modal-evidence-detail'),
  btnCloseEvidenceModal: document.getElementById('btn-close-evidence-modal'),
  btnEvidenceModalClose: document.getElementById('btn-evidence-modal-close'),
  btnCopyEvidenceSha: document.getElementById('btn-copy-evidence-sha'),
  evidenceModalBadge: document.getElementById('evidence-modal-badge'),
  evidenceModalId: document.getElementById('evidence-modal-id'),
  evidenceModalRedaction: document.getElementById('evidence-modal-redaction'),
  evidenceModalTime: document.getElementById('evidence-modal-time'),
  evidenceModalSha: document.getElementById('evidence-modal-sha'),
  evidenceModalSnippet: document.getElementById('evidence-modal-snippet'),

  // Deep Work Bank Modal
  modalBank: document.getElementById('modal-bank-ledger'),
  btnCloseBankModal: document.getElementById('btn-close-bank-modal'),
  btnBankModalGotIt: document.getElementById('btn-bank-modal-got-it'),
  modalBankBalance: document.getElementById('modal-bank-balance'),

  // Toast
  toast: document.getElementById('toast-banner'),
  toastMsg: document.getElementById('toast-message'),
  toastBtn: document.getElementById('toast-action-btn'),
  toastClose: document.getElementById('toast-close-btn'),

  // Permissions & Audit
  grantsTbody: document.getElementById('grants-tbody'),
  btnRevokeAll: document.getElementById('btn-revoke-all'),
  btnExportData: document.getElementById('btn-export-data'),
  btnPurgeData: document.getElementById('btn-purge-data'),
  auditTimeline: document.getElementById('audit-timeline'),
  auditFilter: document.getElementById('audit-filter-select'),
  btnRefreshAudit: document.getElementById('btn-refresh-audit'),

  // Adaptive Routines & Intent Triggers
  routinesTbody: document.getElementById('routines-tbody'),
  routinesCountBadge: document.getElementById('routines-count-badge'),
  btnIntentTired: document.getElementById('btn-intent-tired'),
  btnIntentBreak: document.getElementById('btn-intent-break'),
  btnIntentCustom: document.getElementById('btn-intent-custom'),
};

// Theme Management
function setupTheme() {
  function updateThemeUI(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('aura-color-theme', theme);
    if (DOM.themeToggleLabel) {
      DOM.themeToggleLabel.textContent = theme === 'light' ? 'Dark' : 'Light';
    }

    // High-contrast SVG adjustments for light and dark modes
    const strokePath = document.getElementById('chart-stroke-path');
    const stop0 = document.getElementById('chart-grad-stop-0');
    const stop1 = document.getElementById('chart-grad-stop-1');
    const ringBg = document.querySelector('.progress-ring-bg');

    if (theme === 'light') {
      if (strokePath) strokePath.setAttribute('stroke', '#0F172A');
      if (stop0) {
        stop0.setAttribute('stop-color', '#0F172A');
        stop0.setAttribute('stop-opacity', '0.22');
      }
      if (stop1) {
        stop1.setAttribute('stop-color', '#0F172A');
        stop1.setAttribute('stop-opacity', '0.04');
      }
      if (ringBg) ringBg.setAttribute('stroke', '#E2E8F0');
    } else {
      if (strokePath) strokePath.setAttribute('stroke', '#FFFFFF');
      if (stop0) {
        stop0.setAttribute('stop-color', '#FFFFFF');
        stop0.setAttribute('stop-opacity', '0.30');
      }
      if (stop1) {
        stop1.setAttribute('stop-color', '#FFFFFF');
        stop1.setAttribute('stop-opacity', '0.05');
      }
      if (ringBg) ringBg.setAttribute('stroke', '#1F232B');
    }
  }

  const currentTheme = localStorage.getItem('aura-color-theme') || 'dark';
  updateThemeUI(currentTheme);

  if (DOM.themeToggleBtn) {
    DOM.themeToggleBtn.addEventListener('click', () => {
      const activeTheme = document.documentElement.getAttribute('data-theme') || 'dark';
      const newTheme = activeTheme === 'light' ? 'dark' : 'light';
      updateThemeUI(newTheme);
      if (document.documentElement) document.documentElement.scrollLeft = 0;
      if (document.body) document.body.scrollLeft = 0;
    });
  }
}

// Navigation Tab Management
function setupNavigation() {
  const tabs = document.querySelectorAll('.nav-tab');
  const panels = document.querySelectorAll('.view-panel');

  tabs.forEach((tab) => {
    tab.addEventListener('click', (e) => {
      // If it's an external/page navigation link (like orbit.html), let normal navigation happen
      if (tab.tagName.toLowerCase() === 'a' && tab.getAttribute('href')) {
        return;
      }

      const targetId = tab.getAttribute('aria-controls');
      if (!targetId) return;
      const targetPanel = document.getElementById(targetId);
      if (!targetPanel) return;

      tabs.forEach((t) => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
        t.setAttribute('tabindex', '-1');
      });
      panels.forEach((p) => {
        p.classList.remove('active');
        p.setAttribute('hidden', '');
      });

      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      tab.setAttribute('tabindex', '0');

      targetPanel.classList.add('active');
      targetPanel.removeAttribute('hidden');

      // Close mobile sidebar if open
      const sidebar = document.getElementById('app-sidebar');
      const backdrop = document.getElementById('sidebar-backdrop');
      if (sidebar && sidebar.classList.contains('open')) {
        sidebar.classList.remove('open');
      }
      if (backdrop && backdrop.classList.contains('active')) {
        backdrop.classList.remove('active');
      }

      // Dynamic surface refresh on tab activation
      if (targetId === 'view-permissions' || targetId === 'view-settings') {
        renderGrants();
        renderLearnedRoutines();
        renderGuardrailMetrics();
      }
      if (targetId === 'view-audit') renderAuditTimeline();
      if (targetId === 'view-integrations' || targetId === 'view-products') {
        renderDevicesRegistry();
        renderGuardrailMetrics();
        updateSlackPreview();
      }
      if (targetId === 'view-focus' || targetId === 'view-status') {
        updateTimerUI();
        renderMeeting(state.activeMeetingId);
      }
    });
  });

  // Mobile Sidebar Toggle and Backdrop Handlers
  const mobileToggleBtn = document.getElementById('btn-mobile-sidebar');
  const closeSidebarBtn = document.getElementById('btn-close-sidebar');
  const sidebar = document.getElementById('app-sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');

  function openMobileSidebar() {
    if (sidebar) sidebar.classList.add('open');
    if (backdrop) backdrop.classList.add('active');
  }

  function closeMobileSidebar() {
    if (sidebar) sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('active');
  }

  if (mobileToggleBtn) {
    mobileToggleBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (sidebar && sidebar.classList.contains('open')) {
        closeMobileSidebar();
      } else {
        openMobileSidebar();
      }
    });
  }

  if (closeSidebarBtn) {
    closeSidebarBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeMobileSidebar();
    });
  }

  if (backdrop) {
    backdrop.addEventListener('click', closeMobileSidebar);
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && sidebar && sidebar.classList.contains('open')) {
      closeMobileSidebar();
    }
  });
}

// Render Meeting Content
function renderMeeting(meetingId) {
  const m = meetingsData[meetingId];
  if (!m) return;
  state.activeMeetingId = meetingId;

  // Update chips
  DOM.meetingChips.forEach((chip) => {
    const isTarget = chip.getAttribute('data-meeting') === meetingId;
    chip.classList.toggle('active', isTarget);
    chip.setAttribute('aria-selected', isTarget ? 'true' : 'false');
  });

  // Fade update briefing card
  const meetingCard = document.getElementById('meeting-card');
  if (meetingCard) {
    meetingCard.style.transition = 'opacity 0.15s ease, transform 0.15s ease';
    meetingCard.style.opacity = '0.3';
    meetingCard.style.transform = 'translateY(3px)';
  }

  setTimeout(() => {
    DOM.meetingTime.textContent = m.time;
    DOM.meetingTitle.textContent = m.title;
    DOM.meetingParticipants.textContent = m.participants;
    DOM.meetingToneBadge.textContent = m.tone;
    DOM.briefingWordCount.textContent = m.wordCount;
    DOM.briefingSummary.textContent = m.summary;

    // Render interactive questions
    renderQuestions(m.questions);

    // Render interactive evidence tags
    renderEvidenceTags(m.evidence);

    if (meetingCard) {
      meetingCard.style.opacity = '1';
      meetingCard.style.transform = 'translateY(0)';
    }
  }, 150);
}

// Render Interactive Questions Checklist
function renderQuestions(questions) {
  if (!DOM.briefingQuestionsList) return;
  DOM.briefingQuestionsList.innerHTML = '';

  questions.forEach((q, idx) => {
    const qItem = document.createElement('div');
    qItem.className = 'q-item';
    qItem.innerHTML = `
      <input type="checkbox" class="q-checkbox" id="q-check-${idx}" aria-label="Mark question ${idx + 1} asked">
      <label for="q-check-${idx}" class="q-text">${idx + 1}. ${q}</label>
      <button class="q-copy-btn" title="Copy question to clipboard" aria-label="Copy question ${idx + 1}">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
      </button>
    `;

    // Checkbox toggle
    const chk = qItem.querySelector('.q-checkbox');
    chk.addEventListener('change', () => {
      qItem.classList.toggle('checked', chk.checked);
      if (chk.checked) {
        showToast(`Question ${idx + 1} marked completed.`);
      }
    });

    // Copy action
    const btnCopy = qItem.querySelector('.q-copy-btn');
    btnCopy.addEventListener('click', (e) => {
      e.stopPropagation();
      navigator.clipboard?.writeText(q);
      showToast(`Question ${idx + 1} copied to clipboard!`);
    });

    DOM.briefingQuestionsList.appendChild(qItem);
  });
}

// Render Evidence Tags
function renderEvidenceTags(evidenceKeys) {
  if (!DOM.evidenceTagsContainer) return;
  DOM.evidenceTagsContainer.innerHTML = '';

  evidenceKeys.forEach((key) => {
    const data = evidenceDatabase[key] || {
      badge: "📌 Evidence Source",
      id: key,
    };

    const btn = document.createElement('button');
    btn.className = 'evidence-tag-btn';
    btn.setAttribute('data-evidence', key);
    btn.setAttribute('title', 'Click to inspect cryptographic provenance and redaction record');
    btn.innerHTML = `
      <span class="tag-label">${key}</span>
      <span class="tag-inspect-badge">Inspect</span>
    `;

    btn.addEventListener('click', () => {
      openEvidenceModal(key);
    });

    DOM.evidenceTagsContainer.appendChild(btn);
  });
}

// Open Evidence Inspector Modal
function openEvidenceModal(key) {
  const data = evidenceDatabase[key];
  if (!data || !DOM.modalEvidence) return;

  DOM.evidenceModalBadge.textContent = data.badge;
  DOM.evidenceModalId.textContent = data.id;
  DOM.evidenceModalRedaction.textContent = data.redaction;
  DOM.evidenceModalTime.textContent = data.time;
  DOM.evidenceModalSha.textContent = data.sha;
  DOM.evidenceModalSnippet.textContent = data.snippet;

  DOM.modalEvidence.showModal();
}

// T-5 Countdown Simulation
function startT5Countdown() {
  let seconds = 258; // 4m 18s
  setInterval(() => {
    if (seconds > 0) {
      seconds--;
      const mins = Math.floor(seconds / 60);
      const secs = seconds % 60;
      if (DOM.briefingTimerText) {
        DOM.briefingTimerText.textContent = `T-5 Min Trigger: 0${mins}:${secs < 10 ? '0' : ''}${secs} until sync`;
      }
    }
  }, 1000);
}

// Audio Briefing Player
function toggleWhisperAudio() {
  state.whisperAudioPlaying = !state.whisperAudioPlaying;

  if (state.whisperAudioPlaying) {
    DOM.btnWhisperAudio.classList.add('playing');
    DOM.whisperBtnText.textContent = 'Pause Whisper (42s)';
    DOM.whisperBtnIcon.textContent = '⏸';
    DOM.audioWaveformPlayer.classList.remove('hidden');
    DOM.waveformStatus.textContent = 'Playing private whisper audio via AWS Bedrock...';

    state.whisperTimer = setInterval(() => {
      if (state.whisperSeconds < 42) {
        state.whisperSeconds++;
        DOM.waveformCountdown.textContent = `0:${state.whisperSeconds < 10 ? '0' : ''}${state.whisperSeconds} / 0:42`;
      } else {
        stopWhisperAudio();
      }
    }, 1000);
    showToast('🎧 Playing private haptic voice briefing to wearable.');
  } else {
    stopWhisperAudio();
  }
}

function stopWhisperAudio() {
  state.whisperAudioPlaying = false;
  if (state.whisperTimer) clearInterval(state.whisperTimer);
  DOM.btnWhisperAudio.classList.remove('playing');
  DOM.whisperBtnText.textContent = 'Whisper Brief (42s)';
  DOM.whisperBtnIcon.textContent = '▶';
  DOM.waveformStatus.textContent = 'Paused. Click to resume.';
}

// Focus Timer Logic
function startTimer() {
  if (state.session.timerInterval) clearInterval(state.session.timerInterval);

  state.session.timerInterval = setInterval(() => {
    if (!state.session.active) return;

    if (state.session.remainingSeconds > 0) {
      state.session.remainingSeconds--;
      updateTimerUI();
    } else {
      completeSession();
    }
  }, 1000);
  updateTimerUI();
}

function updateTimerUI() {
  const mins = Math.floor(state.session.remainingSeconds / 60);
  const secs = state.session.remainingSeconds % 60;
  DOM.timerClock.textContent = `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;

  const totalSecs = state.session.durationMinutes * 60;
  const progressRatio = state.session.remainingSeconds / totalSecs;
  // Circumference = 2 * PI * 70 = ~440
  const offset = 440 * (1 - progressRatio);
  DOM.timerProgress.style.strokeDashoffset = offset;
}

function completeSession() {
  clearInterval(state.session.timerInterval);
  state.session.active = false;
  DOM.focusStatusText.textContent = 'Session Completed';
  DOM.btnToggleFocus.textContent = 'Start New Session';
  DOM.btnToggleFocus.classList.remove('btn-primary');
  DOM.btnToggleFocus.classList.add('btn-outline');

  // Award completion points if not distracted
  if (!state.session.isDistracted) {
    const points = state.session.durationMinutes >= 50 ? 4 : 2;
    state.bank.balance += points;
    DOM.bankBalance.textContent = state.bank.balance;
    if (DOM.modalBankBalance) DOM.modalBankBalance.textContent = state.bank.balance;
    showToast(`🎉 Focus session complete! Awarded +${points} Deep Work Bank points.`);
    addAuditRecord('focus_completed', 'Deep Work Bank', 'executed', `Awarded +${points} points for uninterrupted focus`);
  }
}

// Distraction Self-Report Trigger
async function triggerDistraction() {
  if (API_CONFIG.isConnected()) {
    showToast('📡 Transmitting distraction signal to backend policy engine...');
    const resp = await auraApiClient.postFocusSignal({
      user_id: state.userId,
      session_id: state.session.sessionId,
      purpose: 'focus_guardian',
      explicit_signal: 'self_report_distracted',
      consented_signal_score: 0.80,
      user_intent: 'self_report_distracted',
      connected_devices: ['dev_firetv_living_room'],
    });

    if (!resp.success) {
      const errDetail = resp.message || resp.error || `HTTP ${resp.status || 'unknown'}`;
      showToast(`❌ [Backend Error] Distraction signal failed (${errDetail}). No local actions executed.`, false);
      addAuditRecord(
        'focus_signal_failed',
        'Backend Policy Engine',
        'failed',
        `Backend request failed (${errDetail}). Device execution halted.`
      );
      return;
    }

    state.session.isDistracted = true;
    state.session.score = 0.80;
    const reason = 'You explicitly reported being distracted. Proposing to pause allowed media devices.';
    updateScoreUI(state.session.score, reason);

    // Deep work bank penalty: -1 point
    state.bank.balance = Math.max(0, state.bank.balance - 1);
    DOM.bankBalance.textContent = state.bank.balance;
    if (DOM.modalBankBalance) DOM.modalBankBalance.textContent = state.bank.balance;

    const data = resp.data || {};
    const decision = data.decision;
    const undoToken = data.undo_token;

    if (decision === 'executed' && undoToken) {
      state.activeUndoToken = undoToken;
      enableUndoUI(300);
      state.devices.firetv.state = 'paused';
      updateTvUI('paused');
      addRecentActivity('TV paused (Backend Policy)', 'Just now', true);
      addAuditRecord(
        'request_pause',
        'Living Room Fire TV',
        'executed',
        `[Backend Verified] MediaSession pause executed under consent grant. Idempotency token: ${undoToken}`,
        undoToken
      );
      showToast(`✅ [Backend Verified] Fire TV paused. Undo token: ${undoToken} (5m window)`, true);
    } else if (decision === 'suggested_only' || decision === 'ask_user_confirmation') {
      showToast('ℹ️ [Backend Policy] Action requires user confirmation. Device state unchanged.');
      addAuditRecord(
        'request_pause',
        'Living Room Fire TV',
        'suggested_only',
        data.proposal?.reason || 'Policy requires explicit confirmation',
        null
      );
    } else if (decision && decision.startsWith('blocked')) {
      showToast(`🛑 [Policy Blocked] Action blocked: ${data.blocked_reason || decision}`);
      addAuditRecord(
        'request_pause',
        'Living Room Fire TV',
        'blocked',
        `Policy blocked action: ${data.blocked_reason || decision}`,
        null
      );
    } else {
      showToast(`⚠️ [Backend Policy] Unexpected response decision: ${decision || 'none'}. Device state unchanged.`);
    }
    return;
  }

  // Demo / Simulation Mode
  state.session.isDistracted = true;
  state.session.score = 0.80;
  const reason = 'You explicitly reported being distracted. Proposing to pause allowed media devices.';
  updateScoreUI(state.session.score, reason);

  // Deep work bank penalty: -1 point
  state.bank.balance = Math.max(0, state.bank.balance - 1);
  DOM.bankBalance.textContent = state.bank.balance;
  if (DOM.modalBankBalance) DOM.modalBankBalance.textContent = state.bank.balance;

  executeDevicePause();
  showToast('🚨 [Demo Mode] Distraction recorded. Fire TV playback paused locally. Undo available for 5 minutes.', true);
}

function updateScoreUI(score, reason) {
  DOM.scoreVal.textContent = score.toFixed(2);
  DOM.scoreBar.style.width = `${Math.round(score * 100)}%`;
  DOM.scoreReason.textContent = reason;
}

// Undo UI Management
function enableUndoUI(seconds = 30) {
  if (DOM.btnImmediateUndo) DOM.btnImmediateUndo.disabled = false;
  const btnHomeUndo = document.getElementById('btn-home-undo');
  if (btnHomeUndo) btnHomeUndo.disabled = false;

  if (state.undoTimeout) clearTimeout(state.undoTimeout);
  state.undoTimeout = setTimeout(() => {
    if (DOM.btnImmediateUndo) DOM.btnImmediateUndo.disabled = true;
    const bhu = document.getElementById('btn-home-undo');
    if (bhu) bhu.disabled = true;
    state.activeUndoToken = null;
  }, seconds * 1000);
}

// Device Action Execution & Undo
function executeDevicePause() {
  if (API_CONFIG.isConnected()) {
    showToast('⚠️ [Error] Direct device pause is only permitted in Demo/Offline mode.');
    return;
  }
  const undoToken = 'undo_pause_' + Math.random().toString(36).substring(2, 9);
  state.activeUndoToken = undoToken;
  enableUndoUI(300);

  // Update Fire TV state
  state.devices.firetv.state = 'paused';
  updateTvUI('paused');
  if (DOM.btnImmediateUndo) DOM.btnImmediateUndo.disabled = false;
  const btnHomeUndo = document.getElementById('btn-home-undo');
  if (btnHomeUndo) btnHomeUndo.disabled = false;

  addRecentActivity('TV paused (MediaSession)', 'Just now', true);

  addAuditRecord(
    'request_pause',
    'Living Room Fire TV',
    'executed',
    '[Demo Mode] MediaSession transport pause simulated under session grant',
    undoToken
  );
}

async function executeUndo() {
  if (!state.activeUndoToken) return;
  const tokenToUndo = state.activeUndoToken;

  if (API_CONFIG.isConnected()) {
    showToast('⏳ [Backend] Validating and executing undo reversal...');
    const resp = await auraApiClient.undoAction(tokenToUndo);

    if (resp.success && (resp.data?.success || resp.data?.undone)) {
      if (state.undoTimeout) clearTimeout(state.undoTimeout);
      state.devices.firetv.state = 'playing';
      updateTvUI('playing');
      if (DOM.btnImmediateUndo) DOM.btnImmediateUndo.disabled = true;
      const btnHomeUndo = document.getElementById('btn-home-undo');
      if (btnHomeUndo) btnHomeUndo.disabled = true;

      addRecentActivity('TV playback restored (Undo)', 'Just now', true);
      const record = state.audit.find((r) => r.undo_token === tokenToUndo);
      if (record) {
        record.status = 'undone';
        record.reason += ' (Reversed on server)';
      }

      addAuditRecord(
        'undo_reversal',
        'Living Room Fire TV',
        'undone',
        `[Backend Verified] Action reversed: Fire TV resumed playback via token ${tokenToUndo}`,
        null
      );

      state.activeUndoToken = null;
      renderAuditTimeline();
      showToast('↩️ [Backend Verified] Action reversed! Living Room Fire TV resumed playback.');
      return;
    } else {
      const errorMsg = resp.message || resp.data?.message || 'Server rejected undo';
      showToast(`❌ [Undo Failed] ${errorMsg}. Device state was not modified.`, false);
      addAuditRecord(
        'undo_failed',
        'Living Room Fire TV',
        'failed',
        `Undo token ${tokenToUndo} failed on backend: ${errorMsg}`,
        null
      );
      renderAuditTimeline();
      return;
    }
  }

  // Demo / Simulation Mode
  if (state.undoTimeout) clearTimeout(state.undoTimeout);
  state.devices.firetv.state = 'playing';
  updateTvUI('playing');
  if (DOM.btnImmediateUndo) DOM.btnImmediateUndo.disabled = true;
  const btnHomeUndo = document.getElementById('btn-home-undo');
  if (btnHomeUndo) btnHomeUndo.disabled = true;

  addRecentActivity('TV playback restored (Undo)', 'Just now', true);

  const record = state.audit.find((r) => r.undo_token === tokenToUndo);
  if (record) {
    record.status = 'undone';
    record.reason += ' (Reversed locally)';
  }

  addAuditRecord(
    'undo_reversal',
    'Living Room Fire TV',
    'undone',
    `[Demo Mode] Simulated action reversed: Fire TV resumed playback via token ${tokenToUndo}`,
    null
  );

  state.activeUndoToken = null;
  renderAuditTimeline();
  showToast('↩️ [Demo Mode] Action reversed! Living Room Fire TV playback resumed.');
}

// Toast Notifications
function showToast(message, hasUndo = false) {
  DOM.toastMsg.textContent = message;
  DOM.toast.classList.remove('hidden');

  if (hasUndo) {
    DOM.toastBtn.classList.remove('hidden');
    DOM.toastBtn.textContent = 'Undo Now';
    DOM.toastBtn.onclick = () => {
      executeUndo();
      DOM.toast.classList.add('hidden');
    };
  } else {
    DOM.toastBtn.classList.add('hidden');
  }

  setTimeout(() => {
    DOM.toast.classList.add('hidden');
  }, 6000);
}

// Render Grants Table
function renderGrants() {
  DOM.grantsTbody.innerHTML = '';
  state.grants.forEach((grant) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><code>${grant.action_type}</code></td>
      <td>
        <select class="form-select form-select-sm" data-grant-id="${grant.grant_id}">
          <option value="ask_every_time" ${grant.scope === 'ask_every_time' ? 'selected' : ''}>Ask every time</option>
          <option value="allow_session" ${grant.scope === 'allow_session' ? 'selected' : ''}>Allow this session</option>
          <option value="always" ${grant.scope === 'always' ? 'selected' : ''}>Always allow</option>
          <option value="never" ${grant.scope === 'never' ? 'selected' : ''}>Never</option>
        </select>
      </td>
      <td><span class="evidence-tag">${grant.target_allowlist.join(', ') || 'Any supported'}</span></td>
      <td><span class="badge ${grant.active ? 'badge-success' : 'badge-status'}">${grant.active ? 'Active' : 'Revoked'}</span></td>
      <td>
        <button class="btn btn-sm btn-danger-outline btn-revoke" data-grant-id="${grant.grant_id}">Revoke</button>
      </td>
    `;
    DOM.grantsTbody.appendChild(tr);
  });

  // Attach revoke handlers
  document.querySelectorAll('.btn-revoke').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      const gid = e.target.getAttribute('data-grant-id');
      const g = state.grants.find((x) => x.grant_id === gid);
      if (!g) return;

      if (API_CONFIG.isConnected()) {
        showToast(`⏳ [Backend] Revoking grant ${gid}...`);
        const resp = await auraApiClient.revokeGrant(gid);
        if (resp.success) {
          g.active = false;
          renderGrants();
          showToast(`✅ [Backend Verified] Revoked grant ${gid}.`);
          addAuditRecord('grant_revoked', gid, 'executed', `[Backend Verified] Consent grant revoked on server`);
          return;
        } else {
          const errDetail = resp.message || resp.error || `HTTP ${resp.status || 'unknown'}`;
          showToast(`❌ [Backend Error] Failed to revoke grant ${gid}: ${errDetail}. Grant remains active.`, false);
          addAuditRecord('grant_revoke_failed', gid, 'failed', `Backend rejected grant revocation (${errDetail}). Grant remains active.`);
          return;
        }
      }

      g.active = false;
      renderGrants();
      showToast(`[Demo Mode] Revoked grant ${gid} in <60 seconds.`);
      addAuditRecord('grant_revoked', gid, 'executed', `[Demo Mode] Consent grant revoked by user`);
    });
  });
}

// Render Learned Adaptive Intent Routines
function renderLearnedRoutines() {
  if (!DOM.routinesTbody) return;
  DOM.routinesTbody.innerHTML = '';
  const activeCount = state.learnedRoutines.filter((r) => r.active).length;
  if (DOM.routinesCountBadge) {
    DOM.routinesCountBadge.textContent = `${activeCount} Active`;
    DOM.routinesCountBadge.className = `badge ${activeCount > 0 ? 'badge-success' : 'badge-status'}`;
  }

  if (state.learnedRoutines.length === 0) {
    DOM.routinesTbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:16px;">No learned adaptive routines saved.</td></tr>';
    return;
  }

  state.learnedRoutines.forEach((rtn) => {
    const tr = document.createElement('tr');
    const actionsHtml = rtn.actions
      .map((a) => `<code>${a.device}: ${a.action}</code>`)
      .join('<br/>');

    tr.innerHTML = `
      <td><strong>"${rtn.intent_pattern}"</strong></td>
      <td>${rtn.name}</td>
      <td>${actionsHtml}</td>
      <td>
        <select class="form-select form-select-sm routine-scope-select" data-routine-id="${rtn.routine_id}">
          <option value="always" ${rtn.scope === 'always' ? 'selected' : ''}>Always (Auto-Run)</option>
          <option value="allow_session" ${rtn.scope === 'allow_session' ? 'selected' : ''}>This Session</option>
          <option value="ask_every_time" ${rtn.scope === 'ask_every_time' ? 'selected' : ''}>Ask Every Time</option>
        </select>
      </td>
      <td><span class="badge ${rtn.active ? 'badge-success' : 'badge-status'}">${rtn.active ? 'Active' : 'Revoked'}</span></td>
      <td>
        <div style="display:flex; gap:6px; flex-wrap:wrap;">
          <button class="btn btn-sm btn-outline btn-run-routine" data-routine-id="${rtn.routine_id}" title="Execute this learned routine">Run</button>
          <button class="btn btn-sm btn-outline btn-edit-routine" data-routine-id="${rtn.routine_id}" title="Rename or modify routine">Edit</button>
          <button class="btn btn-sm btn-danger-outline btn-revoke-routine" data-routine-id="${rtn.routine_id}">${rtn.active ? 'Revoke' : 'Reactivate'}</button>
        </div>
      </td>
    `;
    DOM.routinesTbody.appendChild(tr);
  });

  // Attach routine handlers
  document.querySelectorAll('.btn-revoke-routine').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const rid = e.target.getAttribute('data-routine-id');
      const r = state.learnedRoutines.find((x) => x.routine_id === rid);
      if (r) {
        r.active = !r.active;
        renderLearnedRoutines();
        showToast(`${r.active ? 'Reactivated' : 'Revoked'} routine "${r.name}".`);
        addAuditRecord(
          r.active ? 'routine_reactivated' : 'routine_revoked',
          r.actions.map((a) => a.device).join(', '),
          'executed',
          `Routine "${r.name}" (${r.intent_pattern}) ${r.active ? 'reactivated' : 'revoked'} by user`,
        );
      }
    });
  });

  document.querySelectorAll('.btn-edit-routine').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const rid = e.target.getAttribute('data-routine-id');
      const r = state.learnedRoutines.find((x) => x.routine_id === rid);
      if (r) {
        const newName = prompt('Edit Routine Name:', r.name);
        if (newName && newName.trim()) {
          r.name = newName.trim();
          renderLearnedRoutines();
          showToast(`Updated routine: "${r.name}"`);
          addAuditRecord('routine_updated', r.routine_id, 'executed', `Renamed routine to "${r.name}"`);
        }
      }
    });
  });

  document.querySelectorAll('.routine-scope-select').forEach((sel) => {
    sel.addEventListener('change', (e) => {
      const rid = e.target.getAttribute('data-routine-id');
      const r = state.learnedRoutines.find((x) => x.routine_id === rid);
      if (r) {
        r.scope = e.target.value;
        showToast(`Updated scope for "${r.name}" to ${r.scope}`);
        addAuditRecord('routine_scope_updated', r.routine_id, 'executed', `Scope set to ${r.scope}`);
      }
    });
  });

  document.querySelectorAll('.btn-run-routine').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const rid = e.target.getAttribute('data-routine-id');
      const r = state.learnedRoutines.find((x) => x.routine_id === rid);
      if (r) {
        executeAdaptiveRoutine(r.intent_pattern, r.actions, r.name);
      }
    });
  });
}

function executeAdaptiveRoutine(intent, actions, routineName = 'Adaptive Routine') {
  const undoToken = 'undo_rtn_' + Math.random().toString(36).substring(2, 9);
  state.activeUndoToken = undoToken;
  enableUndoUI(30);

  const targets = actions.map((a) => a.device).join(', ');
  const actionTypes = actions.map((a) => a.action).join(' + ');

  // If Fire TV is among targets, pause it in state & UI
  if (actions.some((a) => (a.device || '').includes('firetv') && a.action === 'request_pause')) {
    state.devices.firetv.state = 'paused';
    if (DOM.firetvBadge) {
      DOM.firetvBadge.textContent = 'Paused';
      DOM.firetvBadge.className = 'badge badge-status';
    }
    const btnHomePauseTv = document.getElementById('btn-home-pause-tv');
    if (btnHomePauseTv) btnHomePauseTv.textContent = '▶️ Resume Fire TV';
    const tileTvInd = document.getElementById('tile-tv-indicator');
    if (tileTvInd) tileTvInd.style.background = '#f59e0b';
  }

  addRecentActivity(`Routine: ${routineName}`, 'Just now', true);

  addAuditRecord(
    'routine_execution',
    targets,
    'executed',
    `Executed "${routineName}" for intent: "${intent}" across [${targets}] (${actionTypes})`,
    undoToken,
  );

  showToast(`Bedrock routine executed: ${actionTypes} across ${actions.length} devices. Undo ready (30s).`);
}

// Render Audit Timeline
function renderAuditTimeline() {
  const filter = DOM.auditFilter.value;
  DOM.auditTimeline.innerHTML = '';

  const filtered = state.audit.filter((item) => {
    if (filter === 'all') return true;
    if (filter === 'executed') return item.status === 'executed';
    if (filter === 'undone') return item.status === 'undone';
    if (filter === 'suggested_only') return item.status === 'suggested_only';
    if (filter === 'blocked_no_consent') return item.status === 'blocked_no_consent';
    return true;
  });

  if (filtered.length === 0) {
    DOM.auditTimeline.innerHTML = `<p class="timeline-empty" style="color: var(--text-muted); font-size: 0.85rem;">No audit records matching filter "${filter}".</p>`;
    return;
  }

  filtered.forEach((item) => {
    const el = document.createElement('div');
    el.className = `timeline-item ${item.status}`;
    const dateStr = new Date(item.timestamp).toLocaleTimeString();
    el.innerHTML = `
      <div style="flex: 1;">
        <div class="timeline-meta">
          <strong>${item.action_type}</strong>
          <span class="timeline-time">${dateStr}</span>
        </div>
        <div style="font-size: 0.75rem; color: var(--brand-yellow); margin-bottom: 2px;">Target: ${item.target}</div>
        <p class="timeline-reason">${item.reason}</p>
        <span class="badge ${item.status === 'executed' ? 'badge-success' : item.status === 'undone' ? 'badge-status' : 'badge-info'}" style="margin-top: 6px;">
          ${item.status}
        </span>
      </div>
    `;
    DOM.auditTimeline.appendChild(el);
  });
}

function addAuditRecord(actionType, target, status, reason, undoToken = null) {
  state.audit.unshift({
    action_id: 'act_' + Math.random().toString(36).substring(2, 9),
    timestamp: Date.now(),
    action_type: actionType,
    target: target,
    status: status,
    reason: reason,
    undo_token: undoToken,
  });
  renderAuditTimeline();
}

// Event Listeners Setup
function setupEventListeners() {
  // Meeting Switcher Chips
  DOM.meetingChips.forEach((chip) => {
    chip.addEventListener('click', () => {
      const mid = chip.getAttribute('data-meeting');
      renderMeeting(mid);
    });
  });

  // Whisper Audio Button
  if (DOM.btnWhisperAudio) {
    DOM.btnWhisperAudio.addEventListener('click', toggleWhisperAudio);
  }

  // Copy Brief Button
  if (DOM.btnCopyBrief) {
    DOM.btnCopyBrief.addEventListener('click', () => {
      const text = DOM.briefingSummary.textContent.trim();
      navigator.clipboard?.writeText(text);
      showToast('📋 Glanceable briefing copied to clipboard.');
    });
  }

  // Simulate T-5 Trigger
  if (DOM.btnSimulateT5) {
    DOM.btnSimulateT5.addEventListener('click', () => {
      showToast('⚡ T-5 Trigger simulated! Context scrubbed locally and pre-briefing generated.');
      addAuditRecord('simulated_t5', 'Pre-Meeting Engine', 'executed', 'Simulated real-time T-5 trigger for current meeting');
      // Highlight meeting card briefly
      const mc = document.getElementById('meeting-card');
      if (mc) {
        mc.style.boxShadow = '0 0 35px rgba(255, 207, 0, 0.4)';
        setTimeout(() => {
          mc.style.boxShadow = '';
        }, 1200);
      }
    });
  }

  // Deep Work Bank Modal Trigger
  if (DOM.bankBadge) {
    DOM.bankBadge.addEventListener('click', () => {
      if (DOM.modalBankBalance) DOM.modalBankBalance.textContent = state.bank.balance;
      DOM.modalBank.showModal();
    });
  }
  if (DOM.btnCloseBankModal) {
    DOM.btnCloseBankModal.addEventListener('click', () => DOM.modalBank.close());
  }
  if (DOM.btnBankModalGotIt) {
    DOM.btnBankModalGotIt.addEventListener('click', () => DOM.modalBank.close());
  }

  // Evidence Inspector Modal Close
  if (DOM.btnCloseEvidenceModal) {
    DOM.btnCloseEvidenceModal.addEventListener('click', () => DOM.modalEvidence.close());
  }
  if (DOM.btnEvidenceModalClose) {
    DOM.btnEvidenceModalClose.addEventListener('click', () => DOM.modalEvidence.close());
  }
  if (DOM.btnCopyEvidenceSha) {
    DOM.btnCopyEvidenceSha.addEventListener('click', () => {
      const sha = DOM.evidenceModalSha.textContent.replace('SHA-256:', '').trim();
      navigator.clipboard?.writeText(sha);
      showToast('Copied SHA-256 hash to clipboard!');
    });
  }

  // Focus Toggle
  DOM.btnToggleFocus.addEventListener('click', async () => {
    if (state.session.active) {
      if (API_CONFIG.isConnected()) {
        const resp = await auraApiClient.stopSession(state.session.sessionId);
        if (!resp.success) {
          const errDetail = resp.message || resp.error || `HTTP ${resp.status || 'unknown'}`;
          showToast(`❌ [Backend Error] Failed to stop session: ${errDetail}.`, false);
          return;
        }
      }
      completeSession();
    } else {
      if (API_CONFIG.isConnected()) {
        const targetSessionId = 'sess_marcus_' + Math.random().toString(36).substring(2, 9);
        showToast('📡 [Backend] Synchronizing focus session start...');
        const resp = await auraApiClient.startSession(state.session.durationMinutes, targetSessionId);
        if (!resp.success) {
          const errDetail = resp.message || resp.error || `HTTP ${resp.status || 'unknown'}`;
          showToast(`❌ [Backend Error] Failed to start focus session: ${errDetail}. Session not started.`, false);
          addAuditRecord('focus_start_failed', 'Cloud Intake', 'failed', `Backend rejected focus session start (${errDetail})`);
          return;
        }
        state.session.active = true;
        state.session.isDistracted = false;
        state.session.remainingSeconds = state.session.durationMinutes * 60;
        state.session.sessionId = targetSessionId;
        DOM.focusStatusText.textContent = 'Session Active';
        DOM.btnToggleFocus.textContent = 'Stop Focus Session';
        DOM.btnToggleFocus.className = 'btn btn-primary';
        updateScoreUI(0.0, 'Focus session running uninterrupted. Score decays automatically over time.');
        startTimer();
        showToast(`✅ [Backend Verified] Started ${state.session.durationMinutes}m session on cloud intake.`);
        addAuditRecord('focus_started', 'Local Companion', 'executed', `Started ${state.session.durationMinutes}m focus session`);
      } else {
        // Demo Mode
        state.session.active = true;
        state.session.isDistracted = false;
        state.session.remainingSeconds = state.session.durationMinutes * 60;
        state.session.sessionId = 'sess_marcus_' + Math.random().toString(36).substring(2, 9);
        DOM.focusStatusText.textContent = 'Session Active';
        DOM.btnToggleFocus.textContent = 'Stop Focus Session';
        DOM.btnToggleFocus.className = 'btn btn-primary';
        updateScoreUI(0.0, 'Focus session running uninterrupted. Score decays automatically over time.');
        startTimer();
        showToast(`[Demo Mode] Started new ${state.session.durationMinutes}-minute focus session.`);
        addAuditRecord('focus_started', 'Local Companion', 'executed', `Started ${state.session.durationMinutes}m focus session`);
      }
    }
  });

  // Duration Chips
  DOM.durationChips.forEach((chip) => {
    chip.addEventListener('click', () => {
      DOM.durationChips.forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      const dur = parseInt(chip.getAttribute('data-duration'), 10);
      state.session.durationMinutes = dur;
      state.session.remainingSeconds = dur * 60;
      updateTimerUI();
    });
  });

  // Explicit Distraction Button
  DOM.btnReportDistracted.addEventListener('click', triggerDistraction);

  // Adaptive State Intent Buttons
  if (DOM.btnIntentTired) {
    DOM.btnIntentTired.addEventListener('click', () => {
      executeAdaptiveRoutine(
        'I am tired, want to sleep',
        [
          { device: 'dev_firetv_living_room', action: 'request_pause' },
          { device: 'dev_smart_lights', action: 'dim_lights' },
        ],
        'Evening Wind-Down Routine',
      );
    });
  }

  if (DOM.btnIntentBreak) {
    DOM.btnIntentBreak.addEventListener('click', () => {
      executeAdaptiveRoutine(
        'Need a quick break',
        [
          { device: 'dev_firetv_living_room', action: 'request_pause' },
          { device: 'dev_echo_office', action: 'request_dnd' },
        ],
        '15-Min Quick Breather Routine',
      );
    });
  }

  if (DOM.btnIntentCustom) {
    DOM.btnIntentCustom.addEventListener('click', () => {
      const phrase = prompt('Enter your current state or custom intent phrase (e.g., "Brain fog", "Headache"):', 'Brain fog');
      if (phrase && phrase.trim()) {
        executeAdaptiveRoutine(
          phrase.trim(),
          [
            { device: 'dev_firetv_living_room', action: 'request_pause' },
            { device: 'dev_smart_lights', action: 'dim_lights' },
          ],
          `Custom Intent: ${phrase.trim()}`,
        );
      }
    });
  }

  // Test Fire TV Pause
  DOM.btnSimulateAction.addEventListener('click', () => {
    executeDevicePause();
    showToast('Simulated Fire TV pause command sent. Undo is ready.');
  });

  // Immediate Undo
  DOM.btnImmediateUndo.addEventListener('click', executeUndo);

  // Toast Close
  DOM.toastClose.addEventListener('click', () => {
    DOM.toast.classList.add('hidden');
  });

  // Modal: "Why am I seeing this?"
  DOM.btnWhySeeingThis.addEventListener('click', () => {
    DOM.modalWhy.showModal();
  });
  DOM.btnCloseModal.addEventListener('click', () => {
    DOM.modalWhy.close();
  });
  DOM.btnModalGotIt.addEventListener('click', () => {
    DOM.modalWhy.close();
  });

  // Briefing Feedback with Deep Work Bank Points Reward
  DOM.btnHelpful.addEventListener('click', () => {
    state.bank.balance += 2;
    DOM.bankBalance.textContent = state.bank.balance;
    if (DOM.modalBankBalance) DOM.modalBankBalance.textContent = state.bank.balance;
    DOM.btnHelpful.classList.add('active');
    DOM.btnNotUseful.classList.remove('active');
    showToast('👍 Feedback recorded: +2 Bank Points! Stored locally to calibrate future briefs.');
    addAuditRecord('briefing_feedback', 'Preference Calibrator', 'executed', 'Recorded positive feedback (+2 points granted)');
  });

  DOM.btnNotUseful.addEventListener('click', () => {
    DOM.btnNotUseful.classList.add('active');
    DOM.btnHelpful.classList.remove('active');
    showToast('👎 Feedback noted. Will avoid similar pre-meeting context.');
  });

  DOM.btnMuteSeries.addEventListener('click', () => {
    showToast('🔕 Series muted. Pre-meeting briefings silenced for this recurrence.');
    addAuditRecord('series_muted', 'Calendar Scheduler', 'executed', 'Muted recurring event series per user preference');
  });

  // Optional Signals Toggles
  DOM.toggleSignalVideo.addEventListener('change', (e) => {
    const val = e.target.checked;
    showToast(`Local Video Audio Monitor ${val ? 'enabled with explicit consent' : 'disabled'}.`);
  });
  DOM.toggleSignalApp.addEventListener('change', (e) => {
    const val = e.target.checked;
    showToast(`Active Work App Verification ${val ? 'enabled with explicit consent' : 'disabled'}.`);
  });

  // Revoke All
  DOM.btnRevokeAll.addEventListener('click', () => {
    state.grants.forEach((g) => (g.active = false));
    state.learnedRoutines.forEach((r) => (r.active = false));
    renderGrants();
    renderLearnedRoutines();
    showToast('⚠️ All capability grants and learned routines revoked. Any remote execution blocked in <60s.');
    addAuditRecord('all_grants_revoked', 'Policy Engine', 'executed', 'Revoked all user capability grants and learned routines');
  });

  // Export Data (JSON Download)
  DOM.btnExportData.addEventListener('click', () => {
    const exportPayload = {
      user_id: state.userId,
      exported_at: new Date().toISOString(),
      privacy_guarantee: 'ZERO_RAW_TRANSCRIPTS_RETAINED',
      deep_work_bank: {
        balance: state.bank.balance,
        private_only: true,
        employer_reporting: 'STRICTLY_PROHIBITED',
      },
      consent_grants: state.grants,
      learned_routines: state.learnedRoutines,
      audit_records: state.audit,
    };
    const blob = new Blob([JSON.stringify(exportPayload, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `aura_data_export_${state.userId}.json`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('📥 Data export downloaded. Verified zero raw transcripts.');
  });

  // Cryptographic Purge
  DOM.btnPurgeData.addEventListener('click', () => {
    if (confirm('Are you sure you want to cryptographically purge all local AURA records? This action is irreversible.')) {
      state.grants = [];
      state.learnedRoutines = [];
      state.audit = [];
      state.bank.balance = 0;
      DOM.bankBalance.textContent = '0';
      if (DOM.modalBankBalance) DOM.modalBankBalance.textContent = '0';
      renderGrants();
      renderLearnedRoutines();
      renderAuditTimeline();
      showToast('🗑️ All user records cryptographically purged.');
    }
  });

  // Audit Filter & Refresh
  DOM.auditFilter.addEventListener('change', renderAuditTimeline);
  DOM.btnRefreshAudit.addEventListener('click', async () => {
    if (API_CONFIG.isConnected()) {
      showToast('📡 Refreshing audit trail from backend...');
      await syncWithBackend();
    } else {
      renderAuditTimeline();
      showToast('Audit trail refreshed (Demo Mode).');
    }
  });
}

// ==========================================================================
// Phase 5: Connected Devices, Feature Flags & Safety Guardrail Logic
// ==========================================================================

function renderGuardrailMetrics() {
  const statExec = document.getElementById('stat-executions');
  const statUndos = document.getElementById('stat-undos');
  const statRate = document.getElementById('stat-undo-rate');
  const fill = document.getElementById('guardrail-bar-fill');
  const badge = document.getElementById('guardrail-status-badge');
  const msg = document.getElementById('guardrail-alert-msg');
  const prox = document.getElementById('guardrail-proximity-text');

  if (!statExec) return;

  statExec.textContent = state.guardrail.executions;
  statUndos.textContent = state.guardrail.undos;
  const ratePct = (state.guardrail.undoRate * 100).toFixed(1);
  statRate.textContent = `${ratePct}%`;

  if (prox) prox.textContent = `${ratePct}% / 5.0% Max`;

  const barPct = Math.min(100, (state.guardrail.undoRate / state.guardrail.threshold) * 100);
  if (fill) {
    fill.style.width = `${barPct}%`;
  }

  if (state.guardrail.autoExecutionPaused || state.guardrail.undoRate > state.guardrail.threshold) {
    if (statRate) {
      statRate.className = 'metric-val danger';
    }
    if (fill) fill.className = 'guardrail-bar-fill paused';
    if (badge) {
      badge.className = 'badge badge-status';
      badge.textContent = 'Auto-Execution Paused (>5% Undo)';
    }
    if (msg) {
      msg.textContent = '⚠️ Auto-execution paused by safety guardrail! Confirmation required for actions.';
      msg.style.color = 'var(--accent-rose)';
    }
  } else {
    if (statRate) {
      statRate.className = 'metric-val safe';
    }
    if (fill) fill.className = 'guardrail-bar-fill';
    if (badge) {
      badge.className = 'badge badge-success';
      badge.textContent = 'Auto-Execution Active';
    }
    if (msg) {
      msg.textContent = 'System operating normally. Auto-execution enabled.';
      msg.style.color = 'var(--text-secondary)';
    }
  }
}

function renderDevicesRegistry() {
  const tbody = document.getElementById('devices-registry-tbody');
  if (!tbody) return;
  tbody.innerHTML = '';

  state.connectedDevices.forEach((dev) => {
    const tr = document.createElement('tr');
    let certBadgeClass = 'badge-certified';
    let certLabel = 'Certified';
    if (dev.status === 'staged') {
      certBadgeClass = 'badge-staged';
      certLabel = 'Staged Beta';
    } else if (dev.status === 'uncertified') {
      certBadgeClass = 'badge-uncertified';
      certLabel = 'Uncertified';
    }

    let actionBtnHtml = '';
    if (dev.id === 'dev_firetv_living_room' || dev.id === 'dev_firetv_bedroom_staged') {
      actionBtnHtml = `<button class="btn btn-sm btn-outline btn-test-device" data-device-id="${dev.id}" data-action="request_pause">${dev.state === 'paused' ? 'Resume Playback' : 'Pause Playback'}</button>`;
    } else if (dev.type === 'echo_speaker') {
      actionBtnHtml = `<button class="btn btn-sm btn-outline btn-test-device" data-device-id="${dev.id}" data-action="request_dnd">${dev.state === 'dnd_active' ? 'Clear DND' : 'Set DND'}</button>`;
    } else {
      actionBtnHtml = `<button class="btn btn-sm btn-danger-outline btn-test-device" data-device-id="${dev.id}" data-action="request_pause">Test (Fails Safe)</button>`;
    }

    tr.innerHTML = `
      <td><strong>${dev.name}</strong></td>
      <td><code>${dev.id}</code></td>
      <td>
        <span class="badge ${certBadgeClass}">${certLabel}</span>
        <div style="font-size: 0.72rem; color: var(--text-secondary); margin-top: 3px;">${dev.standard}</div>
      </td>
      <td><code>${dev.household}</code></td>
      <td>${dev.rateLimit}</td>
      <td><span class="badge ${dev.state === 'playing' || dev.state === 'dnd_inactive' ? 'badge-success' : dev.state === 'paused' || dev.state === 'dnd_active' ? 'badge-status' : 'badge-info'}">${dev.state}</span></td>
      <td>${actionBtnHtml}</td>
    `;
    tbody.appendChild(tr);
  });

  // Attach test device click handlers
  document.querySelectorAll('.btn-test-device').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const devId = e.currentTarget.getAttribute('data-device-id');
      const action = e.currentTarget.getAttribute('data-action');
      testDeviceAction(devId, action);
    });
  });
}

function testDeviceAction(deviceId, actionType) {
  const dev = state.connectedDevices.find((d) => d.id === deviceId);
  if (!dev) return;

  // 1. Check Global Kill Switch
  const isGlobalEnabled = state.featureFlags.global_switches[actionType] !== false;
  if (!isGlobalEnabled) {
    showToast(`🚫 Blocked: Global Kill Switch for '${actionType}' is tripped!`);
    addAuditRecord(actionType, dev.name, 'blocked_kill_switch', `Halted by Global Kill Switch for ${actionType}`);
    return;
  }

  // 2. Check Safety Guardrail Circuit Breaker (>5% undo rate)
  if (state.guardrail.autoExecutionPaused || state.guardrail.undoRate > state.guardrail.threshold) {
    showToast(`⚠️ Auto-execution paused by safety guardrail! Confirmation prompt issued.`);
    addAuditRecord(actionType, dev.name, 'suggested_only', `Auto-execution paused (Undo rate ${(state.guardrail.undoRate*100).toFixed(1)}% > 5%). Manual confirmation required.`);
    return;
  }

  // 3. Check Certification Status
  if (dev.status === 'uncertified') {
    showToast(`🛑 Blocked: Device is uncertified. Direct remote execution prohibited.`);
    addAuditRecord(actionType, dev.name, 'blocked_device_not_allowed', `Uncertified device ${dev.id} rejected by deterministic policy engine`);
    return;
  }

  // 4. Execute Certified Command
  const prev = dev.state;
  if (actionType === 'request_pause') {
    dev.state = dev.state === 'paused' ? 'playing' : 'paused';
  } else if (actionType === 'request_dnd') {
    dev.state = dev.state === 'dnd_active' ? 'dnd_inactive' : 'dnd_active';
  }

  // Update telemetry
  state.guardrail.executions += 1;
  state.guardrail.undoRate = state.guardrail.undos / state.guardrail.executions;
  renderGuardrailMetrics();
  renderDevicesRegistry();

  const undoToken = `undo_${actionType}_${Math.random().toString(36).substring(2, 9)}`;
  const receipt = {
    receipt_id: `rcpt_${Math.random().toString(36).substring(2, 8)}`,
    idempotency_key: `${state.userId}:${deviceId}:${Date.now()}`,
    device_id: deviceId,
    action: actionType,
    previous_state: prev,
    new_state: dev.state,
    undo_token: undoToken,
    timestamp: new Date().toISOString(),
    certification: dev.standard,
  };
  state.latestReceipt = receipt;

  const receiptBox = document.getElementById('device-receipt-box');
  const receiptContent = document.getElementById('device-receipt-content');
  if (receiptBox && receiptContent) {
    receiptContent.textContent = JSON.stringify(receipt, null, 2);
    receiptBox.style.display = 'block';
  }

  showToast(`⚡ Executed ${actionType} on ${dev.name}. Undo token issued.`, true);
  addAuditRecord(actionType, dev.name, 'executed', `Certified ${actionType} dispatched to ${deviceId}`, undoToken);
}

function setupPhase5EventListeners() {
  // Feature Flag switches
  const togglePause = document.getElementById('toggle-flag-pause');
  if (togglePause) {
    togglePause.addEventListener('change', (e) => {
      state.featureFlags.global_switches.request_pause = e.target.checked;
      showToast(`Global Kill Switch for Fire TV Pause: ${e.target.checked ? 'ENABLED (Normal)' : 'TRIPPED (Halted)'}`);
      addAuditRecord('feature_flag_toggle', 'Global Kill Switch', 'executed', `request_pause set to ${e.target.checked}`);
    });
  }

  const toggleDnd = document.getElementById('toggle-flag-dnd');
  if (toggleDnd) {
    toggleDnd.addEventListener('change', (e) => {
      state.featureFlags.global_switches.request_dnd = e.target.checked;
      showToast(`Global Kill Switch for Echo DND: ${e.target.checked ? 'ENABLED (Normal)' : 'TRIPPED (Halted)'}`);
      addAuditRecord('feature_flag_toggle', 'Global Kill Switch', 'executed', `request_dnd set to ${e.target.checked}`);
    });
  }

  const toggleWhisper = document.getElementById('toggle-flag-whisper');
  if (toggleWhisper) {
    toggleWhisper.addEventListener('change', (e) => {
      state.featureFlags.global_switches.watch_whisper = e.target.checked;
      showToast(`Global Kill Switch for Watch Whisper: ${e.target.checked ? 'ENABLED' : 'TRIPPED'}`);
      addAuditRecord('feature_flag_toggle', 'Global Kill Switch', 'executed', `watch_whisper set to ${e.target.checked}`);
    });
  }

  const toggleSlack = document.getElementById('toggle-flag-slack');
  if (toggleSlack) {
    toggleSlack.addEventListener('change', (e) => {
      state.featureFlags.global_switches.slack_post = e.target.checked;
      showToast(`Global Kill Switch for Slack: ${e.target.checked ? 'ENABLED' : 'TRIPPED'}`);
      addAuditRecord('feature_flag_toggle', 'Global Kill Switch', 'executed', `slack_post set to ${e.target.checked}`);
    });
  }

  // Rollout slider
  const rolloutSlider = document.getElementById('rollout-slider');
  const rolloutLabel = document.getElementById('rollout-percentage-label');
  if (rolloutSlider && rolloutLabel) {
    rolloutSlider.addEventListener('input', (e) => {
      const val = e.target.value;
      rolloutLabel.textContent = `${val}%`;
      state.featureFlags.rollout_percentage = parseInt(val, 10);
    });
  }

  // Guardrail Simulation: Trip Circuit Breaker (>5% undo rate)
  const btnTrip = document.getElementById('btn-test-guardrail-trip');
  if (btnTrip) {
    btnTrip.addEventListener('click', () => {
      state.guardrail.executions = 10;
      state.guardrail.undos = 1;
      state.guardrail.undoRate = 0.10; // 10% > 5%
      state.guardrail.autoExecutionPaused = true;
      renderGuardrailMetrics();
      showToast('⚠️ Guardrail Tripped: Undo rate reached 10.0% (> 5% limit)! Auto-execution paused.');
      addAuditRecord('guardrail_tripped', 'Safety Circuit Breaker', 'suggested_only', 'Undo rate exceeded 5.0% threshold (10.0% detected). Auto-execution paused.');
    });
  }

  // Guardrail Reset Button
  const btnReset = document.getElementById('btn-reset-guardrail-ui');
  if (btnReset) {
    btnReset.addEventListener('click', () => {
      state.guardrail.executions = 12;
      state.guardrail.undos = 0;
      state.guardrail.undoRate = 0.0;
      state.guardrail.autoExecutionPaused = false;
      renderGuardrailMetrics();
      showToast('🔄 Circuit breaker reset. Auto-execution capability restored.');
      addAuditRecord('guardrail_reset', 'Safety Circuit Breaker', 'executed', 'Circuit breaker reset by user/admin. Auto-execution restored.');
    });
  }

  // Refresh devices registry
  const btnRefreshDevs = document.getElementById('btn-refresh-devices-registry');
  if (btnRefreshDevs) {
    btnRefreshDevs.addEventListener('click', () => {
      renderDevicesRegistry();
      showToast('Connected devices registry refreshed.');
    });
  }

  // Live receipt undo & dismiss
  const btnReceiptUndo = document.getElementById('btn-receipt-undo');
  if (btnReceiptUndo) {
    btnReceiptUndo.addEventListener('click', () => {
      if (state.latestReceipt) {
        const dev = state.connectedDevices.find((d) => d.id === state.latestReceipt.device_id);
        if (dev) {
          dev.state = state.latestReceipt.previous_state;
        }
        state.guardrail.undos += 1;
        state.guardrail.undoRate = state.guardrail.undos / state.guardrail.executions;
        if (state.guardrail.executions >= 5 && state.guardrail.undoRate > 0.05) {
          state.guardrail.autoExecutionPaused = true;
        }
        renderGuardrailMetrics();
        renderDevicesRegistry();
        showToast(`↩️ Reversal executed via token ${state.latestReceipt.undo_token}! Restored device state.`);
        addAuditRecord('undo_reversal', state.latestReceipt.device_id, 'undone', `Action reversed via token ${state.latestReceipt.undo_token}`);
        const receiptBox = document.getElementById('device-receipt-box');
        if (receiptBox) receiptBox.style.display = 'none';
      }
    });
  }

  const btnReceiptDismiss = document.getElementById('btn-receipt-dismiss');
  if (btnReceiptDismiss) {
    btnReceiptDismiss.addEventListener('click', () => {
      const receiptBox = document.getElementById('device-receipt-box');
      if (receiptBox) receiptBox.style.display = 'none';
    });
  }
}

function updateSlackPreview() {
  const duration = parseInt(state.slack.durationMinutes, 10) || 45;
  const targetEpoch = Date.now() + duration * 60 * 1000;
  const targetDate = new Date(targetEpoch);
  const hours = String(targetDate.getHours()).padStart(2, '0');
  const minutes = String(targetDate.getMinutes()).padStart(2, '0');
  const previewText = `In focus until ${hours}:${minutes}`;

  state.slack.previewText = previewText;

  const previewTextEl = document.getElementById('slack-preview-text');
  const previewEmojiEl = document.getElementById('slack-preview-emoji');
  const countdownEl = document.getElementById('slack-countdown-text');
  const targetSubtextEl = document.getElementById('slack-target-subtext');

  if (previewTextEl) previewTextEl.textContent = previewText;
  if (previewEmojiEl) previewEmojiEl.textContent = state.slack.previewEmoji || '🎯';
  if (countdownEl) countdownEl.textContent = `Auto-expires in ${duration}m`;
  if (targetSubtextEl) {
    const isChannel = state.slack.selectedChannel.startsWith('#');
    targetSubtextEl.textContent = isChannel
      ? `Target: ${state.slack.selectedChannel} (Allowlisted)`
      : `Target: User Profile Status Icon (Ambient)`;
  }
}

function setupPhase6Slack() {
  const channelSelect = document.getElementById('slack-channel-select');
  if (channelSelect) {
    channelSelect.addEventListener('change', (e) => {
      state.slack.selectedChannel = e.target.value;
      updateSlackPreview();
    });
  }

  const durationSelect = document.getElementById('slack-duration-select');
  if (durationSelect) {
    durationSelect.addEventListener('change', (e) => {
      state.slack.durationMinutes = parseInt(e.target.value, 10);
      updateSlackPreview();
    });
  }

  const btnPublish = document.getElementById('btn-publish-slack');
  if (btnPublish) {
    btnPublish.addEventListener('click', () => {
      // 1. Channel Allowlist Check
      if (state.slack.selectedChannel === '#unapproved-secret-room') {
        showToast('❌ Blocked by Channel Allowlist: Channel #unapproved-secret-room is not in approved allowlist.');
        addAuditRecord('slack_status', '#unapproved-secret-room', 'blocked', 'Blocked by Channel Allowlist: Channel not approved by admin.');
        return;
      }

      // 2. Feature Flag Check
      if (state.featureFlags && state.featureFlags.global_switches && state.featureFlags.global_switches.slack_post === false) {
        showToast('❌ Blocked by Feature Flag: Global kill switch for slack_post is active.');
        addAuditRecord('slack_status', state.slack.selectedChannel, 'blocked', 'Blocked by Tier 1 Global Kill Switch: slack_post is disabled.');
        return;
      }

      // 3. Execution
      const receiptId = 'rcpt_slack_' + Math.random().toString(36).substring(2, 9);
      const undoToken = 'undo_slack_' + Math.random().toString(36).substring(2, 10);
      const targetDuration = state.slack.durationMinutes;

      state.slack.activeReceipt = {
        receipt_id: receiptId,
        undo_token: undoToken,
        status_text: state.slack.previewText,
        channel: state.slack.selectedChannel,
        posted_at: Date.now(),
        expires_at: Date.now() + targetDuration * 60 * 1000,
      };

      // Show receipt in UI
      const activeReceiptRow = document.getElementById('slack-active-receipt');
      const receiptIdEl = document.getElementById('slack-receipt-id');
      if (activeReceiptRow) activeReceiptRow.style.display = 'flex';
      if (receiptIdEl) receiptIdEl.textContent = receiptId;

      showToast(`🚀 Published to Slack: "${state.slack.previewText}" (Auto-expires in ${targetDuration}m)`);
      addAuditRecord('slack_status', state.slack.selectedChannel, 'executed', `Published ambient availability: "${state.slack.previewText}" via OAuth PKCE`, undoToken);
    });
  }

  const btnClear = document.getElementById('btn-clear-slack');
  if (btnClear) {
    btnClear.addEventListener('click', () => {
      if (!state.slack.activeReceipt) {
        showToast('ℹ️ No active Slack status to clear.');
        return;
      }
      state.slack.activeReceipt = null;
      const activeReceiptRow = document.getElementById('slack-active-receipt');
      if (activeReceiptRow) activeReceiptRow.style.display = 'none';

      showToast('🔄 Cleared Slack ambient availability status.');
      addAuditRecord('slack_status_cleared', state.slack.selectedChannel, 'executed', 'Reverted active Slack ambient status to default idle.');
    });
  }

  const btnUndoInline = document.getElementById('btn-undo-slack-inline');
  if (btnUndoInline) {
    btnUndoInline.addEventListener('click', () => {
      if (state.slack.activeReceipt && state.slack.activeReceipt.undo_token) {
        const token = state.slack.activeReceipt.undo_token;
        state.slack.activeReceipt = null;
        const activeReceiptRow = document.getElementById('slack-active-receipt');
        if (activeReceiptRow) activeReceiptRow.style.display = 'none';

        // Increment guardrail undo tracking
        state.guardrail.undos += 1;
        state.guardrail.undoRate = state.guardrail.undos / state.guardrail.executions;
        if (state.guardrail.executions >= 5 && state.guardrail.undoRate > 0.05) {
          state.guardrail.autoExecutionPaused = true;
        }
        renderGuardrailMetrics();

        showToast(`↩️ Slack status reversed immediately via token ${token}!`);
        addAuditRecord('undo_reversal', state.slack.selectedChannel, 'undone', `Slack status reverted via token ${token}`);
      }
    });
  }

  const btnTestLeakage = document.getElementById('btn-test-slack-leakage');
  if (btnTestLeakage) {
    btnTestLeakage.addEventListener('click', () => {
      showToast('🛡️ Blocked: Zero Slack Meeting Leakage Rule violation! Sensitive meeting title detected. Slack posts strictly permit only ambient availability.');
      addAuditRecord('slack_status', state.slack.selectedChannel, 'blocked', 'Zero Slack Meeting Leakage Rule violation: Prohibited fields detected: [\'meeting_summary\', \'3_questions\']');
    });
  }

  updateSlackPreview();
}

// ==========================================================================
// Home Dashboard & Live Telemetry Feed
// ==========================================================================

function addRecentActivity(text, timeStr = 'Just now', isGreen = true) {
  const list = document.getElementById('recent-activity-list');
  if (!list) return;

  const item = document.createElement('div');
  item.className = 'activity-feed-row';
  item.innerHTML = `
    <span class="status-indicator-dot ${isGreen ? 'dot-green' : 'dot-grey'}" aria-hidden="true"></span>
    <span class="activity-feed-desc">${text}</span>
    <span class="activity-feed-time">${timeStr}</span>
  `;
  list.insertBefore(item, list.firstChild);

  // Keep top 6 items
  while (list.children.length > 6) {
    list.removeChild(list.lastChild);
  }
}

function setupHomeDashboard() {
  // Mobile sidebar toggle
  const btnMobileSidebar = document.getElementById('btn-mobile-sidebar');
  const sidebar = document.getElementById('app-sidebar');
  if (btnMobileSidebar && sidebar) {
    btnMobileSidebar.addEventListener('click', () => {
      sidebar.classList.toggle('open');
    });
  }

  // User Profile Dropdown
  const userBtn = document.getElementById('user-profile-btn');
  const userMenu = document.getElementById('user-dropdown-menu');
  if (userBtn && userMenu) {
    userBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      userMenu.classList.toggle('show');
    });
    document.addEventListener('click', () => {
      userMenu.classList.remove('show');
    });
  }

  // User dropdown action links
  const btnUserBank = document.getElementById('btn-user-open-bank');
  if (btnUserBank && DOM.modalBank) {
    btnUserBank.addEventListener('click', () => {
      if (DOM.modalBankBalance) DOM.modalBankBalance.textContent = state.bank.balance;
      DOM.modalBank.showModal();
    });
  }

  const btnUserPurge = document.getElementById('btn-user-purge');
  if (btnUserPurge && DOM.btnPurgeData) {
    btnUserPurge.addEventListener('click', () => {
      DOM.btnPurgeData.click();
    });
  }

  // "View All >" link on Recent Activity Card -> switches to Audit tab
  const linkViewAll = document.getElementById('link-view-all-activity');
  if (linkViewAll) {
    linkViewAll.addEventListener('click', () => {
      const auditTab = document.getElementById('tab-audit');
      if (auditTab) auditTab.click();
    });
  }

  // 4 Device Tiles on Home
  const tileWearable = document.getElementById('tile-device-wearable');
  if (tileWearable) {
    tileWearable.addEventListener('click', () => {
      toggleWhisperAudio();
      addRecentActivity('Wearable briefing triggered', 'Just now', true);
    });
  }

  const tileHub = document.getElementById('tile-device-hub');
  if (tileHub) {
    tileHub.addEventListener('click', () => {
      showToast('📡 Local Hub: Telemetry synced with 4 connected devices.');
      addRecentActivity('Hub synced telemetry', 'Just now', true);
      addAuditRecord('hub_sync', 'Local Hardware Hub', 'executed', 'Manual telemetry sync dispatched to 4 certified endpoints');
    });
  }

  const tileTv = document.getElementById('tile-device-tv');
  if (tileTv) {
    tileTv.addEventListener('click', () => {
      if (state.devices.firetv.state === 'paused') {
        executeUndo();
      } else {
        executeDevicePause();
      }
    });
  }

  const tileAudio = document.getElementById('tile-device-audio');
  if (tileAudio) {
    tileAudio.addEventListener('click', () => {
      const isDnd = state.devices.echo.state === 'dnd_active';
      state.devices.echo.state = isDnd ? 'dnd_inactive' : 'dnd_active';
      const label = isDnd ? 'Echo DND Cleared' : 'Echo DND Activated';
      showToast(`🔊 ${label} for active focus session.`);
      addRecentActivity(label, 'Just now', true);
      addAuditRecord('request_dnd', 'Office Echo Dot', 'executed', `Manual DND toggled to ${state.devices.echo.state}`);
    });
  }

  // Home Quick Action Bar Buttons
  const btnHomeSimT5 = document.getElementById('btn-home-sim-t5');
  if (btnHomeSimT5 && DOM.btnSimulateT5) {
    btnHomeSimT5.addEventListener('click', () => {
      DOM.btnSimulateT5.click();
      addRecentActivity('T-5 Pre-Meeting evaluated', 'Just now', true);
    });
  }

  const btnHomeDistracted = document.getElementById('btn-home-distracted');
  if (btnHomeDistracted) {
    btnHomeDistracted.addEventListener('click', () => {
      triggerDistraction();
    });
  }

  const btnHomePauseTv = document.getElementById('btn-home-pause-tv');
  if (btnHomePauseTv) {
    btnHomePauseTv.addEventListener('click', () => {
      if (state.devices.firetv.state === 'paused') {
        executeUndo();
      } else {
        executeDevicePause();
      }
    });
  }

  const btnHomeUndo = document.getElementById('btn-home-undo');
  if (btnHomeUndo) {
    btnHomeUndo.addEventListener('click', () => {
      executeUndo();
    });
  }

  // Chart range selector filter
  const chartRangeSelect = document.getElementById('chart-range-select');
  const chartStrokePath = document.getElementById('chart-stroke-path');
  const chartAreaPath = document.getElementById('chart-area-path');

  if (chartRangeSelect && chartStrokePath && chartAreaPath) {
    const chartDataVariants = {
      '7d': {
        stroke: 'M 38 168 C 65 168, 75 142, 105 142 C 125 142, 135 158, 155 152 C 175 146, 185 138, 205 138 C 220 138, 230 152, 245 150 C 265 146, 275 110, 295 110 C 310 110, 318 132, 332 142 C 342 150, 350 162, 362 162 C 372 162, 380 135, 395 135 C 410 135, 420 40, 442 40 C 465 40, 475 125, 495 125 C 508 125, 518 165, 532 165 C 545 165, 555 130, 570 120',
        area: 'M 38 170 L 38 168 C 65 168, 75 142, 105 142 C 125 142, 135 158, 155 152 C 175 146, 185 138, 205 138 C 220 138, 230 152, 245 150 C 265 146, 275 110, 295 110 C 310 110, 318 132, 332 142 C 342 150, 350 162, 362 162 C 372 162, 380 135, 395 135 C 410 135, 420 40, 442 40 C 465 40, 475 125, 495 125 C 508 125, 518 165, 532 165 C 545 165, 555 130, 570 120 L 570 170 Z',
        label: 'Showing last 7 days telemetry'
      },
      '30d': {
        stroke: 'M 38 150 C 80 140, 110 110, 150 120 C 190 130, 220 90, 260 85 C 300 80, 340 130, 380 100 C 420 70, 460 30, 500 55 C 530 75, 550 100, 570 95',
        area: 'M 38 170 L 38 150 C 80 140, 110 110, 150 120 C 190 130, 220 90, 260 85 C 300 80, 340 130, 380 100 C 420 70, 460 30, 500 55 C 530 75, 550 100, 570 95 L 570 170 Z',
        label: 'Showing 30 days telemetry'
      },
      '24h': {
        stroke: 'M 38 160 C 100 160, 150 150, 200 130 C 250 110, 300 120, 350 90 C 400 60, 450 50, 500 70 C 535 85, 550 110, 570 105',
        area: 'M 38 170 L 38 160 C 100 160, 150 150, 200 130 C 250 110, 300 120, 350 90 C 400 60, 450 50, 500 70 C 535 85, 550 110, 570 105 L 570 170 Z',
        label: 'Showing last 24 hours telemetry'
      }
    };

    chartRangeSelect.addEventListener('change', (e) => {
      const variant = chartDataVariants[e.target.value];
      if (variant) {
        chartStrokePath.setAttribute('d', variant.stroke);
        chartAreaPath.setAttribute('d', variant.area);
        showToast(`📊 Updated chart view: ${variant.label}`);
      }
    });
  }
}

function initApp() {
  if (document.documentElement) document.documentElement.scrollLeft = 0;
  if (document.body) document.body.scrollLeft = 0;
  setupTheme();
  setupNavigation();
  setupEventListeners();
  setupPhase5EventListeners();
  setupPhase6Slack();
  setupHomeDashboard();
  renderBackendIndicator();
  renderMeeting(state.activeMeetingId);
  renderGrants();
  renderLearnedRoutines();
  renderAuditTimeline();
  renderGuardrailMetrics();
  renderDevicesRegistry();
  startTimer();
  updateScoreUI(0.0, 'Focus session running uninterrupted. Score decays automatically over time.');
  startT5Countdown();

  // If connected, sync initial state from backend
  if (API_CONFIG.isConnected()) {
    syncWithBackend();
  }
}

// Start on DOM Ready
document.addEventListener('DOMContentLoaded', initApp);

