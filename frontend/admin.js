/* ==========================================================================
   admin.js — PodcastShorts AI Admin Portal Script
   Features:
   - Full KPI and sidebar navigation
   - OpenAI-Compatible Custom AI Provider configuration & live testing
   - Real-time live system & user audit event stream with:
     * Request & Response payload inspection
     * Multiple selection & Batch deletion
     * 1-Click Clear all logs with Cyber Neon modal
     * Cyber Neon Pagination (10 / 25 / 50 per page)
     * Severity filtering (SUCCESS / ERROR / WARN / INFO)
   - 1-Click Configured API Key Live Verification (no retyping required)
   - 1-Click Guest Device Trial Reset
   - Custom per-user monthly limit configurator
   - Theme-matching Confirmation Modals for all actions
   ========================================================================== */

let API_BASE = getApiBase();
let currentCustomQuotaUserId = null;
let auditLivePoll = null;

// Audit State & Pagination
let rawAuditEvents = [];
let selectedAuditIds = new Set();
let currentAuditPage = 1;
let currentAuditPerPage = 10;
let currentAuditFilter = 'ALL';

function token() {
  return localStorage.getItem('ps_auth_token') || '';
}

function hdr() {
  const h = { 'Content-Type': 'application/json' };
  const t = token();
  if (t) h['Authorization'] = 'Bearer ' + t;
  return h;
}

let _tz = null;
function _parseAsUTC(iso) {
  if (!iso) return null;
  let s = String(iso).trim();
  // If no timezone info, treat as UTC (server stores UTC)
  if (!/[zZ]|[+-]\d{2}:\d{2}$/.test(s)) {
    // Handle "2026-09-03 10:00:00" or "2026-09-03T10:00:00" without tz
    s = s.replace(' ', 'T');
    if (!s.includes('T')) s += 'T00:00:00';
    s += 'Z';
  }
  return new Date(s);
}
function formatTime(iso) {
  if (!iso) return '—';
  try {
    const d = _parseAsUTC(iso);
    if (!d || isNaN(d)) return '—';
    const opts = { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true };
    if (_tz) opts.timeZone = _tz;
    return d.toLocaleString('en-IN', opts);
  } catch { try { return _parseAsUTC(iso).toLocaleString(); } catch { return String(iso); } }
}
function formatDate(iso) {
  if (!iso) return '—';
  try {
    const d = _parseAsUTC(iso);
    if (!d || isNaN(d)) return '—';
    const opts = { year: 'numeric', month: 'short', day: '2-digit' };
    if (_tz) opts.timeZone = _tz;
    return d.toLocaleDateString('en-IN', opts);
  } catch { return new Date(iso).toLocaleDateString(); }
}

/* ─── Admin Check & Auth Guard ───────────────────────────────────────────── */
async function checkAdmin() {
  const t = token();
  if (!t) {
    showToast('Please sign in to access the Admin Portal.', 'warning');
    setTimeout(() => { location.href = 'login.html?next=admin.html'; }, 800);
    return false;
  }

  showGlobalLoader('Verifying System Owner Credentials...', 'Validating session security and permissions.');

  try {
    const r = await fetch(`${API_BASE}/api/auth/me`, { headers: hdr() });
    if (r.status === 401) throw new Error('SESSION_EXPIRED');
    if (!r.ok) throw new Error('Session invalid');
    const j = await r.json();
    const user = j.user;

    const badge = document.getElementById('adminUserBadge');
    if (badge) badge.textContent = `${user.username} (OWNER)`;

    if (user.role !== 'admin') {
      hideGlobalLoader();
      showToast('Admin permissions required. Redirecting to home dashboard...', 'error');
      setTimeout(() => { location.href = 'index.html'; }, 1200);
      return false;
    }

    hideGlobalLoader();
    return true;
  } catch (e) {
    hideGlobalLoader();
    if (e && e.message === 'SESSION_EXPIRED') {
      clearAuth();
      showToast('Session expired. Please sign in again.', 'warning');
      setTimeout(() => { location.href = 'login.html?next=admin.html'; }, 1000);
      return false;
    }
    if (typeof window.showBackendOfflineOverlay === 'function') {
      window.showBackendOfflineOverlay();
    } else {
      showToast('Unable to reach backend server. Please ensure python server.py is running.', 'error');
    }
    return false;
  }
}

/* ─── Tab Switching (Sidebar Navigation) ─────────────────────────────────── */
function switchTab(name) {
  const titles = {
    config: '⚙️ Config & AI Models',
    prompts: '🧠 Prompts & Pipelines',
    users: '👥 User Accounts Management',
    jobs: '🎬 Pipeline Job Queue',
    pipeline: '🔧 Pipeline Settings',
    quota: '📊 Quota & System Limits',
    audit: '📝 Live System & User Audit Stream'
  };

  document.querySelectorAll('.admin-nav-item-btn').forEach(b => b.classList.remove('active'));
  const targetBtn = document.querySelector(`[data-tab="${name}"]`);
  if (targetBtn) targetBtn.classList.add('active');

  document.querySelectorAll('.admin-panel').forEach(p => p.classList.remove('active'));
  const targetPanel = document.getElementById('panel-' + name);
  if (targetPanel) targetPanel.classList.add('active');

  const titleEl = document.getElementById('currentSectionTitle');
  if (titleEl && titles[name]) {
    titleEl.textContent = titles[name];
  }

  if (typeof toggleAdminSidebar === 'function') {
    toggleAdminSidebar(false);
  }

  if (name === 'config') loadConfig();
  if (name === 'prompts') loadPrompts();
  if (name === 'users') loadUsers();
  if (name === 'jobs') loadJobs();
  if (name === 'pipeline') loadPipelineConfig();
  if (name === 'quota') loadQuota();
  if (name === 'audit') {
    loadAudit();
    loadSystemHealth(false);
    if (!auditLivePoll) {
      auditLivePoll = setInterval(() => {
        if (document.getElementById('panel-audit')?.classList.contains('active')) {
          loadAudit(false);
          loadSystemHealth(false);
        }
      }, 3500);
    }
  }
}

async function loadAll() {
  showGlobalLoader('Refreshing Admin Workspace...', 'Synchronizing keys, prompts, queue, and database tables.');
  try {
    // Load timezone first so all time displays are correct
    await loadTimezone();
    await Promise.all([
      loadConfig(false),
      loadPrompts(false),
      loadUsers(false),
      loadJobs(false),
      loadAudit(false),
      loadQuota(false),
      loadPipelineConfig(),
    ]);
    hideGlobalLoader();
    showToast('All administrative data refreshed successfully.', 'success');
  } catch (e) {
    hideGlobalLoader();
    showToast('Failed to refresh data: ' + e.message, 'warning');
  }
}

async function loadTimezone() {
  try {
    const r = await fetch(`${API_BASE}/api/admin/pipeline-config`, { headers: hdr() });
    const j = await r.json();
    // timezone lives inside config.video_specs (server puts it there)
    const tz = (j.config && j.config.video_specs && j.config.video_specs.timezone) || null;
    if (tz) {
      _tz = tz;
      const tzInput = document.getElementById('pc_timezone');
      if (tzInput) tzInput.value = tz;
    } else {
      _tz = null; // Use viewer's local system timezone
    }
  } catch { }
}

/* ─── Custom OpenAI-Compatible AI Provider Modal Handlers ───────────────── */

function openAddProviderModal() {
  const modal = document.getElementById('addProviderModal');
  if (modal) modal.style.display = 'flex';
  const out = document.getElementById('modal_provider_test_out');
  if (out) out.style.display = 'none';
}

function closeAddProviderModal(e) {
  if (e && e.target && e.target.id !== 'addProviderModal' && !e.target.classList.contains('modal-close-btn')) return;
  const modal = document.getElementById('addProviderModal');
  if (modal) modal.style.display = 'none';
}

async function fetchModelsForModal() {
  const base_url = document.getElementById('modal_provider_base_url')?.value.trim();
  const api_key = document.getElementById('modal_provider_api_key')?.value.trim();
  const sel = document.getElementById('modal_provider_model_select');
  const selBox = document.getElementById('modalModelList');
  const btn = document.getElementById('modalFetchBtn');
  const modelInput = document.getElementById('modal_provider_model');

  if (!base_url) return showToast('Enter Base URL first.', 'warning');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Fetching...'; }

  try {
    const r = await fetch(`${API_BASE}/api/admin/custom-providers/fetch-models`, {
      method: 'POST', headers: hdr(),
      body: JSON.stringify({ base_url, api_key })
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'Failed to fetch models');

    if (sel && selBox) {
      sel.innerHTML = '<option value="">-- Select a model --</option>';
      if (!j.models || j.models.length === 0) {
        sel.innerHTML = '<option value="">No models found at this endpoint</option>';
        showToast('No models returned from provider.', 'warning');
      } else {
        j.models.forEach(m => {
          const opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = m.free ? `🆓 ${m.id}  (FREE)` : `  ${m.id}`;
          sel.appendChild(opt);
        });
        showToast(`Found ${j.count} models! Select one below.`, 'success');
      }
      selBox.style.display = 'block';
      if (modelInput) { modelInput.value = ''; modelInput.placeholder = 'Select from dropdown below ↓'; }
    }
  } catch (e) {
    showToast('Fetch failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔍 Fetch Models'; }
  }
}

function onModelSelected(modelId) {
  const modelInput = document.getElementById('modal_provider_model');
  if (modelInput) modelInput.value = modelId;
}

async function testModalProvider() {
  const base_url = document.getElementById('modal_provider_base_url')?.value.trim();
  const api_key = document.getElementById('modal_provider_api_key')?.value.trim();
  const model_name = document.getElementById('modal_provider_model')?.value.trim();
  const out = document.getElementById('modal_provider_test_out');

  if (!base_url || !model_name) return showToast('Enter Base URL and Model first.', 'warning');

  if (out) {
    out.style.display = 'block';
    out.innerHTML = `<span style="color:var(--cyan);">Testing connection to ${escapeHtml(model_name)}...</span>`;
  }

  showGlobalLoader('Testing AI Connection...', `Connecting to ${model_name}.`);
  try {
    const r = await fetch(`${API_BASE}/api/admin/ai/test`, {
      method: 'POST', headers: hdr(),
      body: JSON.stringify({ base_url, api_key, model_name })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Connection failed');
    if (out) {
      out.innerHTML = `<div style="color:#34d399; font-weight:700; margin-bottom:0.3rem;">✓ ${escapeHtml(j.message)}</div><pre style="color:var(--text-muted); font-size:0.75rem; white-space:pre-wrap;">${escapeHtml(j.response)}</pre>`;
    }
    showToast(`Connected in ${j.latency_ms}ms!`, 'success');
  } catch (e) {
    hideGlobalLoader();
    if (out) out.innerHTML = `<span style="color:#f87171; font-weight:600;">Error: ${escapeHtml(e.message)}</span>`;
    showToast('Connection Error: ' + e.message, 'error');
  }
}

async function submitAddProvider() {
  const name = document.getElementById('modal_provider_name')?.value.trim();
  const base_url = document.getElementById('modal_provider_base_url')?.value.trim();
  const api_key = document.getElementById('modal_provider_api_key')?.value.trim();
  const model = document.getElementById('modal_provider_model')?.value.trim();
  const modelList = document.getElementById('modalModelList');

  if (!name) return showToast('Enter a Provider Name.', 'warning');
  if (!base_url) return showToast('Enter Base URL.', 'warning');
  if (!modelList || modelList.style.display === 'none' || !model) {
    return showToast('Click "Fetch Models" and select a model first.', 'warning');
  }

  showGlobalLoader('Adding Provider...', 'Saving provider configuration.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/custom-providers`, {
      method: 'POST', headers: hdr(),
      body: JSON.stringify({ name, base_url, api_key, model, is_active: true })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed');
    showToast(`Provider "${name}" added and activated!`, 'success');
    closeAddProviderModal();
    loadConfig(false);
  } catch (e) {
    hideGlobalLoader();
    showToast('Add failed: ' + e.message, 'error');
  }
}

async function testCustomProviderCard(id, base_url) {
  const model = document.getElementById(`cp_model_${id}`)?.value.trim();
  const api_key = document.getElementById(`cp_key_${id}`)?.value.trim();
  if (!model) return showToast('Model identifier cannot be empty.', 'warning');

  showGlobalLoader('Verifying Connection...', `Testing connection with ${model}.`);
  try {
    const r = await fetch(`${API_BASE}/api/admin/ai/test`, {
      method: 'POST', headers: hdr(),
      body: JSON.stringify({ base_url, api_key: api_key || '', model_name: model, provider_id: id })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Verification failed');

    const badge = document.getElementById(`badge_cp_${id}`);
    if (badge) {
      badge.className = 'badge badge-green';
      badge.textContent = '● CONFIGURED';
    }
    showToast(`Connection Verified in ${j.latency_ms}ms!`, 'success');
  } catch (e) {
    hideGlobalLoader();
    showToast('Verification failed: ' + e.message, 'error');
  }
}

async function saveCustomProviderCard(id, name, base_url) {
  const model = document.getElementById(`cp_model_${id}`)?.value.trim();
  const api_key = document.getElementById(`cp_key_${id}`)?.value.trim();
  if (!model) return showToast('Model identifier cannot be empty.', 'warning');

  showGlobalLoader('Saving Provider...', `Updating ${name} configuration.`);
  try {
    const payload = { id, name, base_url, model, is_active: true };
    if (api_key) payload.api_key = api_key;

    const r = await fetch(`${API_BASE}/api/admin/custom-providers`, {
      method: 'POST', headers: hdr(),
      body: JSON.stringify(payload)
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed');
    showToast(`Saved ${name} successfully!`, 'success');
    loadConfig(false);
  } catch (e) {
    hideGlobalLoader();
    showToast('Save failed: ' + e.message, 'error');
  }
}

/* ─── Config & Keys ──────────────────────────────────────────────────────── */
async function loadConfig(showLoad = true) {
  if (showLoad) showGlobalLoader('Loading API Keys & Limits...', 'Retrieving active AI configurations.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/config`, { headers: hdr() });
    if (!r.ok) throw new Error(await r.text());
    const j = await r.json();
    const keysDiv = document.getElementById('configKeys');
    const customDiv = document.getElementById('customProvidersList');
    const transDiv = document.getElementById('transcriptionEnginesList');
    const videoDiv = document.getElementById('videoEnginesList');
    if (!keysDiv) return;
    keysDiv.innerHTML = '';
    if (customDiv) customDiv.innerHTML = '';
    if (transDiv) transDiv.innerHTML = '';
    if (videoDiv) videoDiv.innerHTML = '';

    const keys = ['VIDEOSAILOR_API_KEY', 'ASSEMBLYAI_API_KEY', 'GROQ_API_KEY'];
    const keyInfo = {
      VIDEOSAILOR_API_KEY: { title: '🎬 VideoSailor Engine (Optional)', desc: 'Cloud video downloader. Without this key, pipeline uses yt-dlp (FREE, local). Only add if you have a paid VideoSailor subscription.' },
      ASSEMBLYAI_API_KEY: { title: '💬 AssemblyAI Speech Engine', desc: 'Cloud transcription with word-level timestamps and multi-speaker diarization ($0.15-$0.21/hr).' },
      GROQ_API_KEY: { title: '🆓 Groq Whisper Engine (FREE)', desc: 'FREE transcription via Groq Whisper — 8 hrs audio/day, no card needed. Use alongside or instead of AssemblyAI.' }
    };
    // Group mapping for new 3-card layout
    const groupMap = {
      VIDEOSAILOR_API_KEY: videoDiv,
      ASSEMBLYAI_API_KEY: transDiv,
      GROQ_API_KEY: transDiv,
    };

    let setKeysCount = 0;
    keys.forEach(k => {
      const isSet = j.config[k.toLowerCase().replace('_api_key', '')]?.is_set ?? j.config[k]?.is_set ?? false;
      if (isSet === true) setKeysCount++;

      const info = keyInfo[k] || { title: k, desc: 'API key for service integration.' };
      const card = document.createElement('div');
      card.className = 'core-key-card';

      card.innerHTML = `
        <div class="core-key-card-header">
          <div class="core-key-title">${info.title}</div>
          <span id="badge_${k}" class="badge ${isSet === true ? 'badge-green' : 'badge-yellow'}">${isSet === true ? '● CONFIGURED' : '○ NOT SET'}</span>
        </div>
        <p class="core-key-desc">${info.desc}</p>
        <div class="auth-input-container">
          <input class="admin-input" id="cfg_${k}" type="password" placeholder="Enter key (leave empty to test configured)" />
          <button type="button" class="input-icon-btn" onclick="togglePasswordVisibility('cfg_${k}', this)" title="Show/Hide Key">👁️</button>
        </div>
        <div class="core-key-actions">
          <button class="btn-outline btn-sm" onclick="testKey('${k}')" title="Test Live Connection">⚡ Verify Connection</button>
          <button class="btn-primary btn-sm" onclick="saveKey('${k}')">💾 Save</button>
        </div>
      `;
      const target = groupMap[k] || keysDiv;
      (target || keysDiv).appendChild(card);
    });

    // Transcription card: active engine summary
    if (transDiv) {
      const transSummary = document.createElement('div');
      transSummary.className = 'core-key-card';
      transSummary.style.borderLeft = '3px solid #06b6d4';
      transSummary.innerHTML = `
        <div class="core-key-card-header">
          <div class="core-key-title">🎙️ Active Transcription Engine</div>
          <span class="badge badge-green" id="transActiveBadge">—</span>
        </div>
        <p class="core-key-desc">Select active engine in Pipeline → Audio & Engines. Keys saved here are shared.</p>
        <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
          <button class="btn-outline btn-sm" onclick="switchTab('pipeline'); setTimeout(()=>{switchPipelineSubTab('audio', document.querySelector('.pipeline-subnav-pill'));}, 150)">↗ Open Transcription Settings</button>
        </div>
        <div id="transActiveSummary" style="margin-top:0.75rem; font-size:0.82rem; color:var(--text-muted);"></div>
      `;
      transDiv.appendChild(transSummary);
      fetch(`${API_BASE}/api/admin/pipeline-config`, { headers: hdr() }).then(r=>r.json()).then(j=>{
        const cfg=j.config||{};
        const prov=cfg.transcription_provider || 'faster_whisper';
        const el=document.getElementById('transActiveSummary');
        const badge=document.getElementById('transActiveBadge');
        if (el) el.innerHTML=`Active: <strong style="color:#22d3ee;">${escapeHtml(prov)}</strong>`;
        if (badge) badge.textContent=prov;
      }).catch(()=>{});
    }
    // Video card: add Scene Generation link summary (scene providers are in Pipeline)
    if (videoDiv) {
      const sceneLink = document.createElement('div');
      sceneLink.className = 'core-key-card';
      sceneLink.style.borderLeft = '3px solid #f59e0b';
      sceneLink.innerHTML = `
        <div class="core-key-card-header">
          <div class="core-key-title">🎬 Scene Generation (Script-to-Video)</div>
          <span class="badge badge-green">Pipeline Managed</span>
        </div>
        <p class="core-key-desc">Local Wan2.1/LTX, fal.ai, Replicate providers for cinematic backgrounds. Configured in Pipeline → Audio & Engines. Active provider shown below.</p>
        <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
          <button class="btn-outline btn-sm" onclick="switchTab('pipeline'); setTimeout(()=>{switchPipelineSubTab('audio', document.querySelector('.pipeline-subnav-pill')); document.getElementById('pipetab-audio')?.scrollIntoView({behavior:'smooth'});}, 150)">↗ Open Scene Generation</button>
          <button class="btn-outline btn-sm" onclick="refreshVideoCardScene()">🔄 Refresh Scene Providers</button>
        </div>
        <div id="videoCardSceneSummary" style="margin-top:0.75rem; font-size:0.82rem; color:var(--text-muted);">Loading...</div>
      `;
      videoDiv.appendChild(sceneLink);
      // Populate active scene provider summary (live from API, not hardcoded)
      fetch(`${API_BASE}/api/admin/scene-providers`, { headers: hdr() }).then(r=>r.json()).then(j=>{
        const el=document.getElementById('videoCardSceneSummary');
        if (!el) return;
        const active=(j.providers||[]).find(p=>p.is_active);
        if (active) el.innerHTML=`Active: <strong style="color:#fbbf24;">${escapeHtml(active.name)}</strong> (${escapeHtml(active.provider_key)}) — live`;
        else el.innerHTML=`<span style="color:#fbbf24;">⚠️ No scene provider active — fallback to image/template.</span>`;
      }).catch(()=>{ const el=document.getElementById('videoCardSceneSummary'); if(el) el.innerHTML='<span style="color:#f87171;">Failed to load</span>'; });
    }

    // Load custom AI providers from DB into dedicated Custom AI card
    const customTarget = customDiv || keysDiv;
    loadCustomProviders(customTarget, setKeysCount, keys.length);

    const kpiKeys = document.getElementById('kpi-keys');
    if (kpiKeys) kpiKeys.textContent = `${setKeysCount} / ${keys.length}`;

    // Limits & Storage
    const lim = document.getElementById('configLimits');
    if (lim) {
      lim.innerHTML = `
        <div class="admin-card limit-card">
          <div>
            <div class="limit-card-title">FREE_TIER_MONTHLY_LIMIT</div>
            <p class="limit-card-desc">Default shorts allowance per registered free account/month.</p>
          </div>
          <input class="admin-input" id="cfg_FREE_TIER_MONTHLY_LIMIT" type="number" min="0" max="1000" placeholder="5" />
          <button class="btn-primary btn-sm limit-card-btn" onclick="saveKey('FREE_TIER_MONTHLY_LIMIT')">Save Limit</button>
        </div>
        <div class="admin-card limit-card">
          <div>
            <div class="limit-card-title">MAX_VIDEO_DURATION_MINUTES</div>
            <p class="limit-card-desc">Ceiling for input YouTube video length.</p>
          </div>
          <input class="admin-input" id="cfg_MAX_VIDEO_DURATION_MINUTES" type="number" min="1" max="600" placeholder="90" />
          <button class="btn-primary btn-sm limit-card-btn" onclick="saveKey('MAX_VIDEO_DURATION_MINUTES')">Save Duration</button>
        </div>
        <div class="admin-card limit-card">
          <div>
            <div class="limit-card-title">MAX_SHORTS_PER_VIDEO</div>
            <p class="limit-card-desc">Hard cap on how many shorts are generated per YouTube link/video. Leave empty for no limit.</p>
          </div>
          <input class="admin-input" id="cfg_MAX_SHORTS_PER_VIDEO" type="number" min="0" max="50" placeholder="(no limit)" />
          <button class="btn-primary btn-sm limit-card-btn" onclick="saveKey('MAX_SHORTS_PER_VIDEO')">Save Limit</button>
        </div>
        <div class="admin-card limit-card">
          <div>
            <div class="limit-card-title">STORAGE_PATH</div>
            <p class="limit-card-desc">Local folder for storing generated clips.</p>
          </div>
          <input class="admin-input" id="cfg_STORAGE_PATH" placeholder="./storage" />
          <button class="btn-primary btn-sm limit-card-btn" onclick="saveKey('STORAGE_PATH')">Save Path</button>
        </div>
      `;
      // Populate current values from config
      const limEl = document.getElementById('cfg_FREE_TIER_MONTHLY_LIMIT');
      const durEl = document.getElementById('cfg_MAX_VIDEO_DURATION_MINUTES');
      const storEl = document.getElementById('cfg_STORAGE_PATH');
      const shortsEl = document.getElementById('cfg_MAX_SHORTS_PER_VIDEO');
      if (limEl) limEl.value = j.config.free_tier_monthly_limit || '5';
      if (durEl) durEl.value = j.config.max_video_duration_minutes || '90';
      if (storEl) storEl.value = j.config.storage_path || './storage';
      if (shortsEl) shortsEl.value = j.config.max_shorts_per_video || '';
    }

    loadSmtpSettings(false);
    const stat = document.getElementById('adminStatus');
    if (stat) stat.textContent = 'Configuration Synchronized ✓';
  } catch (e) {
    showToast('Failed to load system config: ' + e.message, 'error');
  } finally {
    if (showLoad) hideGlobalLoader();
  }
}

async function testKey(key) {
  const val = document.getElementById('cfg_' + key)?.value.trim() || '';
  const label = key.replace(/_/g, ' ');

  showGlobalLoader(`Verifying ${label}...`, 'Connecting to official provider service and testing API authentication.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/config/test`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ key, value: val })
    });
    const j = await r.json();
    hideGlobalLoader();

    if (!r.ok) throw new Error(j.detail || j.message || 'Verification failed');

    // Update badge in real-time
    const badge = document.getElementById('badge_' + key);
    if (badge) {
      badge.className = `badge ${j.verified ? 'badge-green' : 'badge-yellow'}`;
      badge.textContent = j.verified ? '● CONFIGURED' : '○ NOT SET';
    }

    showToast(j.message, j.verified ? 'success' : 'error');
  } catch (e) {
    hideGlobalLoader();
    showToast('Verification failed: ' + e.message, 'error');
  }
}

async function saveKey(key) {
  const el = document.getElementById('cfg_' + key);
  const val = el?.value.trim();
  // MAX_SHORTS_PER_VIDEO may be cleared (empty = no limit); other keys require a value.
  if (!val && key !== 'MAX_SHORTS_PER_VIDEO') return showToast('Please enter a valid value to save.', 'warning');

  showGlobalLoader(`Saving ${key}...`, 'Updating environment and testing key connection.');
  try {
    const payload = {};
    payload[key] = val;
    const r = await fetch(`${API_BASE}/api/config`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify(payload)
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || j.error || 'Save failed');
    showToast(`Saved ${key} successfully!`, 'success');
    loadConfig(false);
  } catch (e) {
    hideGlobalLoader();
    showToast('Failed to save key: ' + e.message, 'error');
  }
}

/* ─── Custom AI Providers (from DB) ──────────────────────────────────────── */
async function loadCustomProviders(container, standardCount, totalStandard) {
  try {
    const r = await fetch(`${API_BASE}/api/admin/custom-providers`, { headers: hdr() });
    if (!r.ok) return;
    const j = await r.json();
    const providers = j.providers || [];

    const activeCount = standardCount + providers.filter(p => p.is_active).length;
    const kpiKeys = document.getElementById('kpi-keys');
    if (kpiKeys) kpiKeys.textContent = `${activeCount} / ${totalStandard + providers.length}`;

    providers.forEach(p => {
      const card = document.createElement('div');
      card.className = 'core-key-card';
      card.style.borderLeft = p.is_active ? '3px solid var(--green)' : '3px solid var(--border-subtle)';
      card.innerHTML = `
        <div class="core-key-card-header">
          <div class="core-key-title">🤖 ${escapeHtml(p.name)}</div>
          <span id="badge_cp_${p.id}" class="badge ${p.is_active ? 'badge-green' : 'badge-yellow'}">${p.is_active ? '● CONFIGURED' : '○ CONFIGURED'}</span>
        </div>
        <p class="core-key-desc">Drives viral hook scoring, retention analysis, and automatic vertical reframing.</p>
        
        <div style="margin-bottom:0.75rem;">
          <label class="auth-label" style="margin-bottom:0.3rem;">Model</label>
          <input id="cp_model_${p.id}" class="admin-input" value="${escapeHtml(p.model)}" placeholder="e.g. deepseek-chat" style="font-size:0.85rem;" />
        </div>

        <div class="auth-input-container">
          <input class="admin-input" id="cp_key_${p.id}" type="password" placeholder="Enter key (leave empty to test configured)" />
          <button type="button" class="input-icon-btn" onclick="togglePasswordVisibility('cp_key_${p.id}', this)" title="Show/Hide Key">👁️</button>
        </div>

        <div class="core-key-actions">
          <button class="btn-outline btn-sm" onclick="testCustomProviderCard(${p.id}, '${escapeHtml(p.base_url)}')" title="Test Connection">⚡ Verify Connection</button>
          ${p.is_active ? '' : `<button class="btn-outline btn-sm" onclick="activateProvider(${p.id})">✅ Set Active</button>`}
          <button class="btn-primary btn-sm" onclick="saveCustomProviderCard(${p.id}, '${escapeHtml(p.name)}', '${escapeHtml(p.base_url)}')">💾 Save</button>
          <button class="btn-outline btn-sm" onclick="deleteProvider(${p.id}, '${escapeHtml(p.name)}')" style="color:var(--red); border-color:var(--red);" title="Delete Provider">🗑️</button>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (e) {
    console.warn('Failed to load custom providers:', e);
  }
}

async function activateProvider(id) {
  showGlobalLoader('Activating provider...', 'Switching active AI engine.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/custom-providers/${id}/activate`, {
      method: 'POST', headers: hdr()
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed');
    showToast(j.message, 'success');
    loadConfig(false);
  } catch (e) {
    hideGlobalLoader();
    showToast('Error: ' + e.message, 'error');
  }
}

async function deleteProvider(id, name) {
  showConfirmModal({
    title: 'Delete Custom Provider',
    message: `Are you sure you want to delete "${name}"? This cannot be undone.`,
    icon: '🗑️',
    confirmText: 'Delete',
    cancelText: 'Cancel',
    confirmType: 'danger',
    onConfirm: async () => {
      try {
        const r = await fetch(`${API_BASE}/api/admin/custom-providers/${id}`, {
          method: 'DELETE', headers: hdr()
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.detail || 'Failed');
        showToast(j.message, 'success');
        loadConfig(false);
      } catch (e) {
        showToast('Delete failed: ' + e.message, 'error');
      }
    }
  });
}

let rawPrompts = [];

/* ─── Prompts & Pipelines Management ─────────────────────────────────────── */
async function loadPrompts(showLoad = true) {
  if (showLoad) showGlobalLoader('Loading AI Prompt Pipelines...', 'Fetching production prompt templates.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/prompts`, { headers: hdr() });
    const j = await r.json();
    const div = document.getElementById('promptsList');
    if (!div) return;
    div.innerHTML = '';

    const prompts = j.prompts || [];
    rawPrompts = prompts;
    const kpiPrompts = document.getElementById('kpi-prompts');
    if (kpiPrompts) kpiPrompts.textContent = `${prompts.length} Active`;

    // Group by category
    const categoryOrder = ['youtube_shorts', 'script_generation', 'script_based_shorts'];
    const categoryLabels = {
      youtube_shorts: '📱 YouTube Shorts Pipeline',
      script_generation: '✍️ Script Generation',
      script_based_shorts: '🎬 Script-Based Short Creation',
    };
    const grouped = {};
    prompts.forEach(p => {
      const cat = p.category || 'youtube_shorts';
      if (!grouped[cat]) grouped[cat] = [];
      grouped[cat].push(p);
    });

    categoryOrder.forEach(cat => {
      const items = grouped[cat];
      if (!items || items.length === 0) return;

      // Section header
      const header = document.createElement('div');
      header.style.cssText = 'margin-top:1.5rem; margin-bottom:0.75rem; padding-bottom:0.5rem; border-bottom:1px solid rgba(167,139,250,0.3);';
      header.innerHTML = `<strong style="font-size:1.05rem; color:#a78bfa;">${categoryLabels[cat] || cat}</strong><span style="color:var(--text-muted); font-size:0.85rem; margin-left:0.5rem;">(${items.length} pipelines)</span>`;
      div.appendChild(header);

      items.forEach(p => {
        const card = document.createElement('div');
        card.className = 'admin-card';
        card.innerHTML = `
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem;">
            <div style="display:flex; align-items:center; gap:0.6rem;">
              <strong style="font-size:1.05rem; color:#fff;">${escapeHtml(p.name)}</strong>
              <span class="badge ${p.is_active ? 'badge-green' : 'badge-yellow'}">${p.is_active ? '● LIVE PIPELINE' : '○ INACTIVE'}</span>
              <span style="color:var(--text-faint); font-size:0.8rem; margin-left:0.25rem;">temp: ${p.temp}</span>
            </div>
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
              <button class="btn-outline btn-sm" onclick="openEditPromptModal(${p.id})">✏️ Edit</button>
              <button class="btn-outline btn-sm" onclick="testPrompt(${p.id})">🔍 Dry Run</button>
              <button class="btn-outline btn-sm" onclick="testPromptLive(${p.id})">⚡ Live Test</button>
              ${p.is_active ? '' : `<button class="btn-primary btn-sm" onclick="activatePrompt(${p.id})">Activate</button>`}
            </div>
          </div>
          <details style="margin-top:1rem;">
            <summary style="cursor:pointer; color:var(--purple); font-size:0.85rem; font-weight:700;">View System Prompt & User Template</summary>
            <pre style="white-space:pre-wrap; background:#050609; padding:0.9rem; border-radius:8px; font-size:0.82rem; margin-top:0.5rem; border:1px solid var(--border-subtle); max-height:220px; overflow:auto;">${escapeHtml(p.system_prompt)}</pre>
          </details>
        `;
        div.appendChild(card);
      });
    });

    // Render any categories not in the known order so they are never silently dropped
    const extraCats = Object.keys(grouped).filter(cat => !categoryOrder.includes(cat));
    extraCats.forEach(cat => {
      const items = grouped[cat];
      if (!items || items.length === 0) return;
      const header = document.createElement('div');
      header.style.cssText = 'margin-top:1.5rem; margin-bottom:0.75rem; padding-bottom:0.5rem; border-bottom:1px solid rgba(167,139,250,0.3);';
      header.innerHTML = `<strong style="font-size:1.05rem; color:#a78bfa;">🗂️ ${escapeHtml(cat)}</strong><span style="color:var(--text-muted); font-size:0.85rem; margin-left:0.5rem;">(${items.length} pipelines)</span>`;
      div.appendChild(header);
      items.forEach(p => {
        const card = document.createElement('div');
        card.className = 'admin-card';
        card.innerHTML = `
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem;">
            <div style="display:flex; align-items:center; gap:0.6rem;">
              <strong style="font-size:1.05rem; color:#fff;">${escapeHtml(p.name)}</strong>
              <span class="badge ${p.is_active ? 'badge-green' : 'badge-yellow'}">${p.is_active ? '● LIVE PIPELINE' : '○ INACTIVE'}</span>
              <span style="color:var(--text-faint); font-size:0.8rem; margin-left:0.25rem;">temp: ${p.temp}</span>
            </div>
            <div style="display:flex; gap:0.5rem; flex-wrap:wrap;">
              <button class="btn-outline btn-sm" onclick="openEditPromptModal(${p.id})">✏️ Edit</button>
              <button class="btn-outline btn-sm" onclick="testPrompt(${p.id})">🔍 Dry Run</button>
              <button class="btn-outline btn-sm" onclick="testPromptLive(${p.id})">⚡ Live Test</button>
              ${p.is_active ? '' : `<button class="btn-primary btn-sm" onclick="activatePrompt(${p.id})">Activate</button>`}
            </div>
          </div>
        `;
        div.appendChild(card);
      });
    });
  } catch (e) {
    showToast('Failed to load prompts: ' + e.message, 'error');
  } finally {
    if (showLoad) hideGlobalLoader();
  }
}

function openEditPromptModal(promptId) {
  const p = rawPrompts.find(item => item.id === promptId);
  if (!p) return;

  const idEl = document.getElementById('editPromptId');
  const nameEl = document.getElementById('editPromptName');
  const sysEl = document.getElementById('editPromptSystem');
  const userEl = document.getElementById('editPromptUser');
  const tempEl = document.getElementById('editPromptTemp');
  const activeEl = document.getElementById('editPromptActive');
  const titleEl = document.getElementById('editPromptModalTitle');

  if (idEl) idEl.value = p.id;
  if (nameEl) nameEl.value = p.name;
  if (sysEl) sysEl.value = p.system_prompt;
  if (userEl) userEl.value = p.user_template || '';
  if (tempEl) tempEl.value = p.temp ?? 0.1;
  if (activeEl) activeEl.value = p.is_active ? 'true' : 'false';
  if (titleEl) titleEl.textContent = `✏️ Edit Prompt: ${p.name}`;

  const modal = document.getElementById('editPromptModal');
  if (modal) modal.style.display = 'flex';
}

function closeEditPromptModal(e) {
  if (e && e.target && e.target.id !== 'editPromptModal' && !e.target.classList.contains('modal-close-btn') && !e.target.classList.contains('btn-outline')) return;
  const modal = document.getElementById('editPromptModal');
  if (modal) modal.style.display = 'none';
}

async function submitEditPrompt() {
  const id = document.getElementById('editPromptId')?.value;
  const name = document.getElementById('editPromptName')?.value.trim();
  const system_prompt = document.getElementById('editPromptSystem')?.value.trim();
  const user_template = document.getElementById('editPromptUser')?.value.trim();
  const temp = parseFloat(document.getElementById('editPromptTemp')?.value || '0.1');
  const is_active = document.getElementById('editPromptActive')?.value === 'true';

  if (!id || !system_prompt) {
    return showToast('System prompt instructions cannot be empty.', 'warning');
  }

  showGlobalLoader('Saving AI Prompt Changes...', 'Updating production database.');
  try {
    const res = await fetch(`${API_BASE}/api/admin/prompts/${id}`, {
      method: 'PUT',
      headers: hdr(),
      body: JSON.stringify({ name, system_prompt, user_template, temp, is_active })
    });
    const data = await res.json();
    hideGlobalLoader();

    if (!res.ok) throw new Error(data.detail || 'Failed to update prompt');
    showToast(data.message || 'Prompt updated successfully!', 'success');
    closeEditPromptModal();
    loadPrompts(false);
  } catch (err) {
    hideGlobalLoader();
    showToast('Failed to save prompt: ' + err.message, 'error');
  }
}

async function testPrompt(id) {
  showGlobalLoader('Testing Prompt Template...', 'Running dry run template interpolation.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/prompts/${id}/test`, { method: 'POST', headers: hdr() });
    const j = await r.json();
    hideGlobalLoader();
    showToast('Template rendered successfully!', 'success');
    showConfirmModal({
      title: 'Prompt Dry-Run Output',
      message: j.rendered_preview || 'No preview generated',
      icon: '🔍',
      confirmText: 'Done',
      cancelText: 'Close'
    });
  } catch (e) {
    hideGlobalLoader();
    showToast('Dry run failed: ' + e.message, 'error');
  }
}

async function testPromptLive(id) {
  showGlobalLoader('Executing Live AI Inference...', 'Connecting to LLM provider for live response.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/prompts/${id}/test?live=true`, { method: 'POST', headers: hdr() });
    const j = await r.json();
    hideGlobalLoader();
    showToast('Live AI inference completed.', 'success');
    showConfirmModal({
      title: 'Live AI LLM Response',
      message: j.live_llm_response || j.live_llm_error || 'No response',
      icon: '⚡',
      confirmText: 'Done',
      cancelText: 'Close'
    });
  } catch (e) {
    hideGlobalLoader();
    showToast('Live test failed: ' + e.message, 'error');
  }
}

async function activatePrompt(id) {
  showGlobalLoader('Activating Prompt Template...', 'Updating active production AI prompt.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/prompts/${id}/activate`, { method: 'POST', headers: hdr() });
    const j = await r.json();
    hideGlobalLoader();
    showToast('AI prompt activated for production generation!', 'success');
    loadPrompts(false);
  } catch (e) {
    hideGlobalLoader();
    showToast('Failed to activate prompt: ' + e.message, 'error');
  }
}

/* ─── Users Data Table & Management ───────────────────────────────────────── */
async function loadUsers(showLoad = true) {
  if (showLoad) showGlobalLoader('Loading Users Database...', 'Retrieving registered account records.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/users`, { headers: hdr() });
    const j = await r.json();
    const tbody = document.getElementById('usersTable');
    if (!tbody) return;
    tbody.innerHTML = '';

    const users = j.users || [];
    const regularCount = j.total_regular_users ?? users.filter(u => u.role !== 'admin').length;
    const kpiUsers = document.getElementById('kpi-users');
    if (kpiUsers) kpiUsers.textContent = `${regularCount} Active`;

    users.forEach(u => {
      const isOwner = (u.id === 1 || u.username.toLowerCase() === 'admin');
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td style="font-weight:700; color:var(--text-muted);">${u.id}</td>
        <td>
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <strong>${escapeHtml(u.username)}</strong>
            ${isOwner ? '<span class="badge badge-purple" style="font-size:0.68rem;">SYSTEM OWNER</span>' : ''}
          </div>
        </td>
        <td style="color:var(--text-muted);">${escapeHtml(u.email)}</td>
        <td>
          <span class="badge ${u.is_active ? 'badge-green' : 'badge-red'}">
            ${u.is_active ? '● Active' : '○ Suspended'}
          </span>
        </td>
        <td>
          <span class="badge ${isOwner ? 'badge-purple' : 'badge-green'}">${isOwner ? 'Owner' : escapeHtml(u.tier || 'free')}</span>
        </td>
        <td style="color:var(--text-muted); font-size:0.82rem;">${formatDate(u.created_at)}</td>
        <td style="text-align:right;">
          ${isOwner ? '<span style="color:var(--text-faint); font-size:0.8rem; font-style:italic;">Protected Owner</span>' : `
            <div style="display:inline-flex; gap:0.4rem;">
              <button class="btn-outline btn-sm" onclick="toggleUserStatus(${u.id}, '${escapeHtml(u.username)}', ${Boolean(u.is_active)})" title="Toggle Active/Suspended Status">
                ${u.is_active ? '⏸️ Suspend' : '▶️ Activate'}
              </button>
              <button class="btn-outline btn-sm" onclick="openUserQuotaModal(${u.id}, '${escapeHtml(u.username)}')" title="Set Custom Monthly Quota">
                🎯 Set Limit
              </button>
              <button class="btn-outline btn-sm btn-danger-outline" onclick="deleteUserAccount(${u.id}, '${escapeHtml(u.username)}')" title="Delete Account Permanently">
                🗑️
              </button>
            </div>
          `}
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    showToast('Failed to load user accounts: ' + e.message, 'error');
  } finally {
    if (showLoad) hideGlobalLoader();
  }
}

function toggleUserStatus(userId, username, isActive) {
  const newStatus = isActive ? 'suspend' : 'activate';
  showConfirmModal({
    title: `${isActive ? 'Suspend' : 'Activate'} User Account`,
    message: `Are you sure you want to ${newStatus} account for "${username}"? ${isActive ? 'They will not be able to log in or generate shorts.' : 'Their account access will be immediately restored.'}`,
    icon: isActive ? '⏸️' : '▶️',
    confirmText: isActive ? 'Suspend User' : 'Activate User',
    confirmType: isActive ? 'danger' : 'primary',
    onConfirm: async () => {
      showGlobalLoader('Updating User Status...', 'Syncing permissions.');
      try {
        const res = await fetch(`${API_BASE}/api/admin/users/${userId}/status`, {
          method: 'POST',
          headers: hdr()
        });
        const data = await res.json();
        hideGlobalLoader();
        if (!res.ok) throw new Error(data.detail || 'Failed to update user status');
        showToast(data.message || 'User status updated', 'success');
        loadUsers(false);
      } catch (err) {
        hideGlobalLoader();
        showToast(err.message, 'error');
      }
    }
  });
}

function deleteUserAccount(userId, username) {
  showConfirmModal({
    title: 'Delete User Account Permanently',
    message: `Are you sure you want to permanently delete user "${username}"? All their queue jobs, quotas, and account data will be permanently wiped. This action CANNOT be undone.`,
    icon: '🗑️',
    confirmText: 'Delete User Forever',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Deleting User Account...', 'Purging user records and quotas.');
      try {
        const res = await fetch(`${API_BASE}/api/admin/users/${userId}`, {
          method: 'DELETE',
          headers: hdr()
        });
        const data = await res.json();
        hideGlobalLoader();
        if (!res.ok) throw new Error(data.detail || 'Failed to delete user');
        showToast(`User account "${username}" deleted successfully.`, 'success');
        loadUsers(false);
      } catch (err) {
        hideGlobalLoader();
        showToast(err.message, 'error');
      }
    }
  });
}

function openUserQuotaModal(userId, username) {
  currentCustomQuotaUserId = userId;
  const nameEl = document.getElementById('quotaModalUsername');
  if (nameEl) nameEl.textContent = username;
  const modal = document.getElementById('userQuotaModal');
  if (modal) modal.style.display = 'flex';
}

function closeUserQuotaModal(e) {
  if (e && e.target && e.target.id !== 'userQuotaModal' && !e.target.classList.contains('modal-close-btn')) return;
  const modal = document.getElementById('userQuotaModal');
  if (modal) modal.style.display = 'none';
}

async function submitCustomQuota() {
  const limit = parseInt(document.getElementById('customQuotaInput')?.value || '10', 10);
  if (!limit || limit < 1) return showToast('Please enter a valid monthly limit.', 'warning');

  showGlobalLoader('Setting Custom Limit...', 'Saving user quota.');
  try {
    const res = await fetch(`${API_BASE}/api/admin/users/${currentCustomQuotaUserId}/quota`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ limit })
    });
    const data = await res.json();
    hideGlobalLoader();
    if (!res.ok) throw new Error(data.detail || 'Failed to set quota');
    showToast(data.message, 'success');
    closeUserQuotaModal();
  } catch (err) {
    hideGlobalLoader();
    showToast(err.message, 'error');
  }
}

/* ─── SMTP Email Server Settings ─────────────────────────────────────────── */
async function loadSmtpSettings(showLoad = false) {
  if (showLoad) showGlobalLoader('Loading SMTP Settings...', 'Fetching email configuration.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/smtp`, { headers: hdr() });
    const j = await r.json();
    if (j.success && j.smtp) {
      const s = j.smtp;
      if (document.getElementById('smtp_host')) document.getElementById('smtp_host').value = s.host || '';
      if (document.getElementById('smtp_port')) document.getElementById('smtp_port').value = s.port || 587;
      if (document.getElementById('smtp_username')) document.getElementById('smtp_username').value = s.username || '';
      if (document.getElementById('smtp_password') && s.password) document.getElementById('smtp_password').value = s.password;
      if (document.getElementById('smtp_sender_email')) document.getElementById('smtp_sender_email').value = s.sender_email || '';
      if (document.getElementById('smtp_sender_name')) document.getElementById('smtp_sender_name').value = s.sender_name || 'Vergeclip AI Security';
      const pwEl = document.getElementById('smtp_password');
      if (pwEl) {
        pwEl.value = '';
        pwEl.placeholder = s.password ? '•••••••• saved password — leave blank to keep' : 'Enter SMTP password';
      }
    }
  } catch (e) {
    // optional
  } finally {
    if (showLoad) hideGlobalLoader();
  }
}

async function saveSmtpSettings() {
  const host = document.getElementById('smtp_host')?.value.trim();
  const port = parseInt(document.getElementById('smtp_port')?.value || '587', 10);
  const username = document.getElementById('smtp_username')?.value.trim();
  const password = document.getElementById('smtp_password')?.value;
  const sender_email = document.getElementById('smtp_sender_email')?.value.trim();
  const sender_name = document.getElementById('smtp_sender_name')?.value.trim();

  if (!host || !sender_email) {
    return showToast('Please enter both SMTP Host and Sender Email Address.', 'warning');
  }

  showGlobalLoader('Saving SMTP Settings...', 'Configuring email transmission service.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/smtp/save`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ host, port, username, password, sender_email, sender_name, use_tls: true })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed to save SMTP');
    showToast(j.message || 'SMTP Settings saved successfully!', 'success');
  } catch (e) {
    hideGlobalLoader();
    showToast('Failed to save SMTP: ' + e.message, 'error');
  }
}

async function testSmtpConnection() {
  const test_email = document.getElementById('smtp_test_email')?.value.trim();
  if (!test_email || !test_email.includes('@')) {
    return showToast('Please enter a valid destination email address to test.', 'warning');
  }

  showGlobalLoader('Sending Test Email...', `Dispatching SMTP test message to ${test_email}`);
  try {
    const r = await fetch(`${API_BASE}/api/admin/smtp/test`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ test_email })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Test email failed');
    showToast(j.message || 'Test email sent successfully!', 'success');
  } catch (e) {
    hideGlobalLoader();
    showToast('SMTP Test Error: ' + e.message, 'error');
  }
}

/* ─── System Health Diagnostics ───────────────────────────────────────────── */
async function loadSystemHealth(showLoad = false) {
  if (showLoad) showGlobalLoader('Diagnosing System Resources...', 'Reading CPU, RAM, and Disk metrics.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/system-health`, { headers: hdr() });
    const j = await r.json();
    if (j.success && j.health) {
      const h = j.health;
      const statusEl = document.getElementById('healthStatus');
      const cpuEl = document.getElementById('healthCpu');
      const memEl = document.getElementById('healthMem');
      const diskEl = document.getElementById('healthDisk');
      const dbEl = document.getElementById('healthDb');

      if (statusEl) statusEl.textContent = `● ${h.status}`;
      if (cpuEl) cpuEl.textContent = `${h.cpu_usage_pct}%`;
      if (memEl) memEl.textContent = `${h.memory_usage_pct}% (${h.memory_used_mb}MB)`;
      if (diskEl) diskEl.textContent = `${h.disk_free_gb} GB`;
      if (dbEl) dbEl.textContent = `${h.db_size_mb} MB`;
    }
  } catch (e) {
    // silent fallback
  } finally {
    if (showLoad) hideGlobalLoader();
  }
}

/* ─── Paginated Job Queue Management with Multi-Select Delete ─────────────── */
let currentJobPage = 1;
let currentJobPerPage = 10;
let currentJobSearch = '';
let currentJobStatus = 'all';
let selectedJobIds = new Set();
let jobSearchTimer = null;

function debounceJobSearch() {
  clearTimeout(jobSearchTimer);
  jobSearchTimer = setTimeout(() => {
    currentJobSearch = document.getElementById('jobSearchInput')?.value.trim().toLowerCase() || '';
    loadJobs(1, false);
  }, 300);
}

function changeJobPerPage(val) {
  currentJobPerPage = parseInt(val, 10) || 10;
  loadJobs(1, false);
}

function toggleSelectAllJobs(checked) {
  document.querySelectorAll('.job-row-checkbox').forEach(cb => {
    cb.checked = checked;
    if (checked) selectedJobIds.add(cb.dataset.id);
    else selectedJobIds.delete(cb.dataset.id);
  });
  updateJobBatchButton();
}

function toggleSingleJobSelect(cb) {
  if (cb.checked) selectedJobIds.add(cb.dataset.id);
  else selectedJobIds.delete(cb.dataset.id);

  const all = document.querySelectorAll('.job-row-checkbox');
  const checked = document.querySelectorAll('.job-row-checkbox:checked');
  const master = document.getElementById('selectAllJobs');
  if (master) master.checked = (all.length > 0 && all.length === checked.length);
  updateJobBatchButton();
}

function updateJobBatchButton() {
  const btn = document.getElementById('btnDeleteJobsBatch');
  const countEl = document.getElementById('selectedJobsCount');
  if (btn && countEl) {
    countEl.textContent = selectedJobIds.size;
    btn.style.display = selectedJobIds.size > 0 ? 'inline-flex' : 'none';
  }
}

async function loadJobs(page = 1, showLoad = true) {
  if (typeof page === 'boolean') {
    showLoad = page;
    page = 1;
  }
  currentJobPage = parseInt(page, 10) || 1;
  currentJobStatus = document.getElementById('jobStatusFilter')?.value || 'all';
  if (showLoad) showGlobalLoader('Loading Job Queue...', 'Fetching queued and processing video tasks.');

  const params = new URLSearchParams({
    page: currentJobPage,
    limit: currentJobPerPage,
    status: currentJobStatus,
    search: currentJobSearch
  });

  try {
    const r = await fetch(`${API_BASE}/api/admin/jobs?${params}`, { headers: hdr() });
    const j = await r.json();
    const tbody = document.getElementById('jobsTable');
    if (!tbody) return;
    tbody.innerHTML = '';

    const jobs = j.jobs || [];
    const total = j.total ?? jobs.length;
    const totalPages = j.total_pages || Math.ceil(total / currentJobPerPage) || 1;

    const kpiJobs = document.getElementById('kpi-jobs');
    if (kpiJobs) kpiJobs.textContent = `${total} Total`;

    if (jobs.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="8" style="text-align:center; padding:2rem; color:var(--text-muted);">
            No video generation jobs found matching the active filter.
          </td>
        </tr>
      `;
    } else {
      jobs.forEach(jb => {
        const tr = document.createElement('tr');
        const badge = jb.status === 'completed' || jb.status === 'done' ? 'badge-green' : jb.status === 'failed' || jb.status === 'error' ? 'badge-red' : 'badge-yellow';
        const isChecked = selectedJobIds.has(jb.id);
        const failedAt = (jb.status === 'failed' || jb.status === 'error') && jb.progress_percent > 0;
        const barColor = failedAt ? 'var(--red)' : '';
        const phaseNames = { 5: 'Init', 25: 'Download', 30: 'Transcribe Start', 45: 'Transcribe Done', 50: 'Clip Select', 65: 'Clip Scored', 70: 'Semantic Rank', 75: 'Render Start', 100: 'Complete' };
        let phaseLabel = '';
        if (failedAt) {
          const closest = Object.keys(phaseNames).map(Number).filter(v => v <= jb.progress_percent).sort((a, b) => b - a)[0];
          phaseLabel = phaseNames[closest] || '';
        }

        tr.innerHTML = `
          <td><input type="checkbox" class="job-row-checkbox" data-id="${jb.id}" ${isChecked ? 'checked' : ''} onchange="toggleSingleJobSelect(this)" /></td>
          <td style="font-family:var(--font-mono); font-weight:700; color:#fff;">${escapeHtml(String(jb.id).slice(0, 8))}</td>
          <td><span class="badge badge-purple" style="font-size:0.7rem;">${jb.job_type === 'script_to_video' ? '📝 Script' : '🎬 YouTube'}</span></td>
          <td><span class="badge badge-purple" style="font-size:0.75rem;">${escapeHtml(jb.user_name || 'System')}</span></td>
          <td style="max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(jb.youtube_url || '')}">
            ${escapeHtml(jb.youtube_url || '—')}
          </td>
          <td><span class="badge ${badge}">● ${escapeHtml(jb.status)}</span></td>
          <td>
            <div style="display:flex; align-items:center; gap:0.5rem;">
              <div class="progress-track" style="height:6px; margin:0; width:70px;">
                <div class="progress-bar" style="width:${jb.progress_percent || 0}%;${barColor ? 'background:' + barColor + ';' : ''}"></div>
              </div>
              <span style="font-size:0.75rem; font-weight:700;${failedAt ? 'color:var(--red);' : ''}">${jb.progress_percent || 0}%${failedAt && phaseLabel ? ' <span style="color:var(--text-muted);font-weight:400;font-size:0.65rem;">(' + phaseLabel + ')</span>' : ''}</span>
            </div>
          </td>
          <td style="color:var(--text-muted); font-size:0.8rem;">${formatTime(jb.created_at)}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    // Render Pagination
    const infoEl = document.getElementById('jobPaginationInfo');
    if (infoEl) {
      const startIdx = total === 0 ? 0 : (currentJobPage - 1) * currentJobPerPage + 1;
      const endIdx = Math.min(currentJobPage * currentJobPerPage, total);
      infoEl.textContent = `Showing ${startIdx}–${endIdx} of ${total} jobs`;
    }

    const controlsEl = document.getElementById('jobPaginationControls');
    if (controlsEl) {
      controlsEl.innerHTML = '';
      if (totalPages > 1) {
        const prevBtn = document.createElement('button');
        prevBtn.className = 'btn-outline btn-sm';
        prevBtn.style.padding = '0.2rem 0.6rem';
        prevBtn.disabled = currentJobPage <= 1;
        prevBtn.textContent = '◀ Prev';
        prevBtn.onclick = () => loadJobs(currentJobPage - 1, false);
        controlsEl.appendChild(prevBtn);

        const pageLabel = document.createElement('span');
        pageLabel.style.fontSize = '0.85rem';
        pageLabel.style.color = '#fff';
        pageLabel.style.margin = '0 0.5rem';
        pageLabel.textContent = `Page ${currentJobPage} of ${totalPages}`;
        controlsEl.appendChild(pageLabel);

        const nextBtn = document.createElement('button');
        nextBtn.className = 'btn-outline btn-sm';
        nextBtn.style.padding = '0.2rem 0.6rem';
        nextBtn.disabled = currentJobPage >= totalPages;
        nextBtn.textContent = 'Next ▶';
        nextBtn.onclick = () => loadJobs(currentJobPage + 1, false);
        controlsEl.appendChild(nextBtn);
      }
    }
  } catch (e) {
    showToast('Failed to load queue jobs: ' + e.message, 'error');
  } finally {
    if (showLoad) hideGlobalLoader();
  }
}

function deleteSelectedJobs() {
  if (selectedJobIds.size === 0) return;
  const list = Array.from(selectedJobIds);

  showConfirmModal({
    title: `Delete ${list.length} Selected Job(s)`,
    message: `Are you sure you want to permanently delete ${list.length} selected job record(s) from the pipeline queue?`,
    icon: '🗑️',
    confirmText: `Yes, Delete ${list.length} Job(s)`,
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Deleting Selected Jobs...', 'Purging queue records.');
      try {
        const r = await fetch(`${API_BASE}/api/admin/jobs/batch`, {
          method: 'DELETE',
          headers: hdr(),
          body: JSON.stringify({ job_ids: list })
        });
        const j = await r.json();
        hideGlobalLoader();
        if (!r.ok) throw new Error(j.detail || 'Delete failed');
        selectedJobIds.clear();
        updateJobBatchButton();
        showToast(j.message || 'Jobs deleted successfully.', 'success');
        loadJobs(currentJobPage, false);
      } catch (e) {
        hideGlobalLoader();
        showToast('Batch Delete Error: ' + e.message, 'error');
      }
    }
  });
}

function clearCompletedJobs() {
  showConfirmModal({
    title: 'Purge Finished Jobs',
    message: 'Are you sure you want to remove all completed and failed jobs from the queue? Active jobs will not be affected.',
    icon: '🧹',
    confirmText: 'Yes, Purge Queue',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Cleaning Job Queue...', 'Removing finished tasks.');
      try {
        const r = await fetch(`${API_BASE}/api/admin/jobs/clear`, {
          method: 'POST',
          headers: hdr()
        });
        const j = await r.json();
        hideGlobalLoader();
        if (!r.ok) throw new Error(j.detail || 'Purge failed');
        selectedJobIds.clear();
        updateJobBatchButton();
        showToast(j.message || 'Finished jobs cleared.', 'success');
        loadJobs(1, false);
      } catch (e) {
        hideGlobalLoader();
        showToast('Clear Queue Error: ' + e.message, 'error');
      }
    }
  });
}

/* ─── Real-Time Live System & User Audit Log Stream with Pagination ───────── */
async function loadAudit(showLoad = false) {
  if (showLoad) showGlobalLoader('Fetching Real-Time Audit Logs...', 'Streaming events.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/audit`, { headers: hdr() });
    const j = await r.json();
    rawAuditEvents = j.live_events || [];
    renderAuditTable();
  } catch (e) {
    // Non-critical audit stream
  } finally {
    if (showLoad) hideGlobalLoader();
  }
}

function filterAuditLogs() {
  currentAuditFilter = document.getElementById('auditSeverityFilter')?.value || 'ALL';
  currentAuditPage = 1;
  renderAuditTable();
}

function changeAuditPerPage(val) {
  currentAuditPerPage = parseInt(val, 10) || 10;
  currentAuditPage = 1;
  renderAuditTable();
}

function renderAuditTable() {
  const tbody = document.getElementById('liveAuditTable');
  if (!tbody) return;
  tbody.innerHTML = '';

  // 1. Filter
  let filtered = rawAuditEvents;
  if (currentAuditFilter !== 'ALL') {
    filtered = rawAuditEvents.filter(ev => ev.severity === currentAuditFilter);
  }

  // 2. Pagination Math
  const total = filtered.length;
  const totalPages = Math.ceil(total / currentAuditPerPage) || 1;
  if (currentAuditPage > totalPages) currentAuditPage = totalPages;
  const startIdx = (currentAuditPage - 1) * currentAuditPerPage;
  const pageItems = filtered.slice(startIdx, startIdx + currentAuditPerPage);

  // Update Pagination Info
  const infoEl = document.getElementById('auditPaginationInfo');
  if (infoEl) {
    infoEl.textContent = total > 0 ? `Showing ${startIdx + 1} - ${Math.min(startIdx + currentAuditPerPage, total)} of ${total} events` : 'No events found';
  }

  // Render Table Rows
  if (!pageItems.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:2rem;">No system audit events match current filter.</td></tr>`;
  } else {
    pageItems.forEach(ev => {
      const isSelected = selectedAuditIds.has(ev.id);
      const tr = document.createElement('tr');
      const sevClass = ev.severity === 'SUCCESS' ? 'badge-green' : ev.severity === 'ERROR' ? 'badge-red' : ev.severity === 'WARN' ? 'badge-yellow' : 'badge-purple';
      const timeStr = formatTime(ev.timestamp);
      const hasPayload = Boolean(ev.request_data || ev.response_data);

      tr.innerHTML = `
        <td><input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectAudit('${escapeHtml(String(ev.id))}', this.checked)" /></td>
        <td style="color:var(--text-muted); font-family:var(--font-mono); font-size:0.82rem;">${timeStr}</td>
        <td><span class="badge ${sevClass}">${escapeHtml(ev.severity)}</span></td>
        <td><span class="badge" style="background:rgba(255,255,255,0.06);">${escapeHtml(ev.category)}</span></td>
        <td><strong style="font-size:0.88rem; color:#fff;">${escapeHtml(ev.action)}</strong></td>
        <td style="color:var(--text-main); font-size:0.85rem; max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(ev.detail)}">${escapeHtml(ev.detail)}</td>
        <td style="color:var(--text-muted); font-size:0.82rem;">${escapeHtml(ev.user_id)} (${escapeHtml(ev.ip)})</td>
        <td style="text-align:right;">
          <button class="btn-outline btn-sm" onclick="openAuditModal('${escapeHtml(String(ev.id))}')" style="padding:0.25rem 0.5rem; font-size:0.75rem;">
            🔍 Inspect
          </button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  // 3. Render Pagination Controls
  renderAuditPaginationButtons(totalPages);
  updateAuditSelectionUI();
}

function renderAuditPaginationButtons(totalPages) {
  const container = document.getElementById('auditPaginationControls');
  if (!container) return;
  container.innerHTML = '';

  if (totalPages <= 1) return;

  const prevBtn = document.createElement('button');
  prevBtn.className = 'btn-outline btn-sm';
  prevBtn.textContent = '‹ Prev';
  prevBtn.disabled = currentAuditPage === 1;
  prevBtn.onclick = () => { currentAuditPage--; renderAuditTable(); };
  container.appendChild(prevBtn);

  for (let p = 1; p <= totalPages; p++) {
    if (p === 1 || p === totalPages || (p >= currentAuditPage - 1 && p <= currentAuditPage + 1)) {
      const pageBtn = document.createElement('button');
      pageBtn.className = p === currentAuditPage ? 'btn-primary btn-sm' : 'btn-outline btn-sm';
      pageBtn.textContent = String(p);
      pageBtn.onclick = () => { currentAuditPage = p; renderAuditTable(); };
      container.appendChild(pageBtn);
    } else if (p === currentAuditPage - 2 || p === currentAuditPage + 2) {
      const dots = document.createElement('span');
      dots.textContent = '...';
      dots.style.color = 'var(--text-muted)';
      dots.style.padding = '0 0.3rem';
      container.appendChild(dots);
    }
  }

  const nextBtn = document.createElement('button');
  nextBtn.className = 'btn-outline btn-sm';
  nextBtn.textContent = 'Next ›';
  nextBtn.disabled = currentAuditPage === totalPages;
  nextBtn.onclick = () => { currentAuditPage++; renderAuditTable(); };
  container.appendChild(nextBtn);
}

function toggleSelectAudit(id, checked) {
  if (checked) {
    selectedAuditIds.add(id);
  } else {
    selectedAuditIds.delete(id);
  }
  updateAuditSelectionUI();
}

function toggleSelectAllAudit(checked) {
  if (checked) {
    rawAuditEvents.forEach(ev => selectedAuditIds.add(ev.id));
  } else {
    selectedAuditIds.clear();
  }
  renderAuditTable();
}

function updateAuditSelectionUI() {
  const count = selectedAuditIds.size;
  const btn = document.getElementById('btnDeleteAuditBatch');
  const countEl = document.getElementById('selectedAuditCount');
  if (countEl) countEl.textContent = count;
  if (btn) {
    btn.style.display = count > 0 ? 'inline-flex' : 'none';
  }
}

function deleteSelectedAuditLogs() {
  const ids = Array.from(selectedAuditIds);
  if (!ids.length) return;

  showConfirmModal({
    title: `Delete Selected Logs (${ids.length})`,
    message: `Are you sure you want to permanently delete these ${ids.length} selected audit log records from memory?`,
    icon: '🗑️',
    confirmText: 'Delete Selected',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Deleting Audit Logs...', 'Removing selected records.');
      try {
        const res = await fetch(`${API_BASE}/api/admin/audit/batch`, {
          method: 'DELETE',
          headers: hdr(),
          body: JSON.stringify({ ids })
        });
        const data = await res.json();
        hideGlobalLoader();
        if (!res.ok) throw new Error(data.detail || 'Failed to delete logs');
        showToast(data.message || 'Deleted logs', 'success');
        selectedAuditIds.clear();
        loadAudit(false);
      } catch (err) {
        hideGlobalLoader();
        showToast(err.message, 'error');
      }
    }
  });
}

function clearAllAuditLogs() {
  showConfirmModal({
    title: 'Purge All Audit Logs',
    message: 'Are you sure you want to clear all real-time system and user audit log records? This cannot be recovered.',
    icon: '⚠️',
    confirmText: 'Yes, Clear Everything',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Clearing All Audit Logs...', 'Flushing event buffer.');
      try {
        const res = await fetch(`${API_BASE}/api/admin/audit/clear`, {
          method: 'POST',
          headers: hdr()
        });
        const data = await res.json();
        hideGlobalLoader();
        if (!res.ok) throw new Error(data.detail || 'Failed to clear logs');
        showToast(data.message || 'All audit logs cleared', 'success');
        selectedAuditIds.clear();
        loadAudit(false);
      } catch (err) {
        hideGlobalLoader();
        showToast(err.message, 'error');
      }
    }
  });
}

function openAuditModal(eventId) {
  const ev = rawAuditEvents.find(e => e.id === eventId);
  if (!ev) return;

  const titleEl = document.getElementById('auditModalTitle');
  const headerEl = document.getElementById('auditModalHeader');
  const reqEl = document.getElementById('auditModalReq');
  const respEl = document.getElementById('auditModalResp');

  if (titleEl) titleEl.textContent = `📝 Audit Event #${ev.id}`;
  if (headerEl) {
    headerEl.innerHTML = `
      <span class="badge ${ev.severity === 'SUCCESS' ? 'badge-green' : ev.severity === 'ERROR' ? 'badge-red' : 'badge-yellow'}">${ev.severity}</span>
      <span style="margin-left:0.5rem; color:#fff;">[${escapeHtml(ev.category)}] ${escapeHtml(ev.action)}</span>
      <div style="font-size:0.85rem; color:var(--text-muted); margin-top:0.3rem;">${escapeHtml(ev.detail)}</div>
    `;
  }
  if (reqEl) {
    let reqStr = 'No request payload recorded for this action.';
    if (ev.request_data) {
      try { reqStr = typeof ev.request_data === 'object' ? JSON.stringify(ev.request_data, null, 2) : JSON.stringify(JSON.parse(ev.request_data), null, 2); } catch { reqStr = String(ev.request_data); }
    }
    reqEl.textContent = reqStr;
  }
  if (respEl) {
    let respStr = ev.severity === 'SUCCESS' ? 'Operation completed with HTTP 200 OK ✓' : 'Event recorded with no response payload.';
    if (ev.response_data) {
      try { respStr = typeof ev.response_data === 'object' ? JSON.stringify(ev.response_data, null, 2) : JSON.stringify(JSON.parse(ev.response_data), null, 2); } catch { respStr = String(ev.response_data); }
    }
    respEl.textContent = respStr;
  }

  const modal = document.getElementById('auditDetailModal');
  if (modal) modal.style.display = 'flex';
}

function closeAuditModal(e) {
  if (e && e.target && e.target.id !== 'auditDetailModal' && !e.target.classList.contains('modal-close-btn') && !e.target.classList.contains('btn-primary')) return;
  const modal = document.getElementById('auditDetailModal');
  if (modal) modal.style.display = 'none';
}

/* ─── Quota & Limits Analytics ────────────────────────────────────────────── */
async function loadQuota(showLoad = true) {
  try {
    const r = await fetch(`${API_BASE}/api/user/quota`, { headers: hdr() });
    const j = await r.json();
    const div = document.getElementById('quotaInfo');
    if (!div) return;
    const q = j.quota || {};

    div.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:0.75rem;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span style="font-weight:700;">Billing Cycle: ${q.month_year || 'Current Month'}</span>
          <span class="badge badge-purple" style="font-size:0.85rem;">Unlimited Allowance (System Owner)</span>
        </div>
        <div class="progress-track" style="height:10px;">
          <div class="progress-bar" style="width:100%; background:var(--grad-primary);"></div>
        </div>
        <div style="display:flex; justify-content:space-between; font-size:0.85rem; color:var(--text-muted);">
          <span>Processed this month: <strong>${q.videos_processed || 0} shorts</strong></span>
          <span>Status: <strong style="color:#34d399;">Active (No Restrictions)</strong></span>
        </div>
      </div>
    `;
  } catch (e) {
    const div = document.getElementById('quotaInfo');
    if (div) div.textContent = 'Failed to retrieve quota analytics';
  }
}

function resetGuestTrials() {
  showConfirmModal({
    title: 'Reset All Guest Device Trials',
    message: 'Are you sure you want to reset all guest device trial fingerprints and IP records? All unregistered visitors will immediately receive a fresh 1-video trial allowance.',
    icon: '🔄',
    confirmText: 'Reset Guest Trials',
    confirmType: 'primary',
    onConfirm: async () => {
      showGlobalLoader('Resetting Guest Trials...', 'Clearing trial locks and database records.');
      try {
        const res = await fetch(`${API_BASE}/api/admin/trials/reset`, {
          method: 'POST',
          headers: hdr()
        });
        const data = await res.json();
        hideGlobalLoader();
        if (!res.ok) throw new Error(data.detail || 'Failed to reset trials');
        showToast(data.message, 'success');
        loadAudit(false);
      } catch (err) {
        hideGlobalLoader();
        showToast(err.message, 'error');
      }
    }
  });
}

/* ─── In-App Password Change Modal Helper ─────────────────────────────────── */
function openChangePasswordModal() {
  const modal = document.getElementById('changePasswordModal');
  if (modal) modal.style.display = 'flex';
}

function closeChangePasswordModal(e) {
  if (e && e.target && e.target.id !== 'changePasswordModal' && !e.target.classList.contains('modal-close-btn')) return;
  const modal = document.getElementById('changePasswordModal');
  if (modal) modal.style.display = 'none';
}

async function submitChangePassword() {
  const old_password = document.getElementById('cp_old_password')?.value;
  const new_password = document.getElementById('cp_new_password')?.value;
  const confirm_password = document.getElementById('cp_confirm_password')?.value;

  if (!old_password) return showToast('Please enter your current password.', 'warning');
  if (!new_password || new_password.length < 8) return showToast('New password must be at least 8 characters long.', 'warning');
  if (new_password !== confirm_password) return showToast('New passwords do not match. Please re-enter.', 'warning');

  showGlobalLoader('Updating Password...', 'Encrypting new security credentials.');
  try {
    const r = await fetch(`${API_BASE}/api/auth/change-password`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ old_password, new_password })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed to change password');

    showToast('Admin password changed successfully!', 'success');
    closeChangePasswordModal();
    if (document.getElementById('cp_old_password')) document.getElementById('cp_old_password').value = '';
    if (document.getElementById('cp_new_password')) document.getElementById('cp_new_password').value = '';
    if (document.getElementById('cp_confirm_password')) document.getElementById('cp_confirm_password').value = '';
  } catch (err) {
    hideGlobalLoader();
    showToast(err.message || 'Failed to change password', 'error');
  }
}

/* ─── Pipeline Settings (Dynamic Configuration) ─────────────────────────── */

const PIPELINE_FIELDS = [
  // Video Specs
  'target_width', 'target_height', 'target_fps', 'max_short_duration', 'min_short_duration', 'timezone',
  // Clip Selection
  'clip_min_duration', 'clip_max_duration', 'clip_top_n', 'clip_min_score', 'clip_min_separation',
  'clip_step_size', 'clip_overlap_threshold', 'clip_distribution_strategy',
  // Semantic Ranking
  'semantic_default_pool_size', 'semantic_min_score', 'semantic_default_top_n', 'semantic_default_separation',
  // Captions
  'caption_font_size', 'caption_max_words', 'caption_min_words', 'caption_max_lines', 'caption_max_width', 'caption_y',
  'caption_text_color', 'caption_highlight_color', 'caption_outline_color', 'caption_outline_width',
  'caption_start_padding', 'caption_end_padding', 'caption_max_duration', 'caption_min_duration',
  // Enhancement
  'auto_color_filter_enabled', 'auto_video_filter', 'auto_pitch_shift_enabled', 'auto_pitch_semitones',
];

const BOOL_FIELDS = ['auto_color_filter_enabled', 'auto_pitch_shift_enabled'];

function switchPipelineSubTab(subTab, btn) {
  const pills = document.querySelectorAll('.pipeline-subnav-pill');
  pills.forEach(p => p.classList.remove('active'));
  if (btn) btn.classList.add('active');

  const panes = document.querySelectorAll('.pipetab-pane');
  if (subTab === 'all') {
    panes.forEach(pane => pane.classList.add('active'));
  } else {
    panes.forEach(pane => {
      if (pane.id === `pipetab-${subTab}`) {
        pane.classList.add('active');
      } else {
        pane.classList.remove('active');
      }
    });
  }
}

async function loadPipelineConfig() {
  try {
    const r = await fetch(`${API_BASE}/api/admin/pipeline-config`, { headers: hdr() });
    const j = await r.json();
    if (!r.ok || !j.success) throw new Error(j.detail || 'Failed to load pipeline config');
    const cfg = j.config;

    // Flatten all sections into one object
    const flat = {};
    for (const section of Object.values(cfg)) {
      if (section && typeof section === 'object' && !Array.isArray(section)) {
        Object.assign(flat, section);
      }
    }

    // Populate fields
    for (const key of PIPELINE_FIELDS) {
      const el = document.getElementById(`pc_${key}`);
      if (!el) continue;
      const val = flat[key];
      if (val === null || val === undefined) continue;
      if (BOOL_FIELDS.includes(key)) {
        el.checked = val === true || val === 'true' || val === '1';
      } else {
        el.value = val;
      }
    }

    // System prompt
    const promptEl = document.getElementById('pc_pipeline_system_prompt');
    if (promptEl) promptEl.value = cfg.pipeline_system_prompt || '';

    // Transcription provider
    const tpEl = document.getElementById('pc_transcription_provider');
    if (tpEl && cfg.transcription_provider) tpEl.value = cfg.transcription_provider;
    const gmEl = document.getElementById('pc_groq_whisper_model');
    if (gmEl && cfg.groq_whisper_model) gmEl.value = cfg.groq_whisper_model;
    const fwEl = document.getElementById('pc_faster_whisper_model');
    if (fwEl && cfg.faster_whisper_model) fwEl.value = cfg.faster_whisper_model;
    toggleTranscriptionOptions();

    // Scene generation — providers loaded from the scene_providers API (not pipeline config)
    loadSceneProviders();
    loadTemplateBackgrounds();

    const cuEl = document.getElementById('pc_comfyui_url');
    if (cuEl && cfg.comfyui_url) cuEl.value = cfg.comfyui_url;
    toggleSceneGenOptions();

    // Update timezone for time display
    const tzEl = document.getElementById('pc_timezone');
    if (tzEl && cfg.video_specs && cfg.video_specs.timezone) {
      _tz = cfg.video_specs.timezone;
      tzEl.value = cfg.video_specs.timezone;
    }

    // Scoring weights (nested in cfg, not flattened)
    const weights = (cfg.scoring_weights && typeof cfg.scoring_weights === 'object')
      ? cfg.scoring_weights
      : (flat.scoring_weights || {});
    renderScoringWeights(weights);
  } catch (err) {
    console.error('Failed to load pipeline config:', err);
  }
}

function renderScoringWeights(weights) {
  const grid = document.getElementById('scoringWeightsGrid');
  if (!grid) return;
  grid.innerHTML = '';
  for (const [key, val] of Object.entries(weights)) {
    const div = document.createElement('div');
    div.innerHTML = `<label class="auth-label">${escapeHtml(key)}</label><input class="admin-input" type="number" step="0.5" data-weight-key="${escapeHtml(key)}" value="${escapeHtml(val)}" />`;
    grid.appendChild(div);
  }
}

function collectScoringWeights() {
  const weights = {};
  document.querySelectorAll('[data-weight-key]').forEach(el => {
    weights[el.dataset.weightKey] = parseFloat(el.value) || 0;
  });
  return weights;
}

async function savePipelineConfig() {
  const payload = {};
  for (const key of PIPELINE_FIELDS) {
    const el = document.getElementById(`pc_${key}`);
    if (!el) continue;
    if (BOOL_FIELDS.includes(key)) {
      payload[key] = el.checked ? 'true' : 'false';
    } else {
      const val = el.value;
      if (val !== '' && val !== null && val !== undefined) {
        payload[key] = val;
      }
    }
  }
  // Scoring weights as JSON (only if weight inputs exist in DOM)
  const weights = collectScoringWeights();
  if (Object.keys(weights).length > 0) {
    payload.scoring_weights = JSON.stringify(weights);
  }
  // System prompt
  const promptEl = document.getElementById('pc_pipeline_system_prompt');
  if (promptEl) payload.pipeline_system_prompt = promptEl.value;
  // Transcription engine
  const tpEl = document.getElementById('pc_transcription_provider');
  if (tpEl) payload.transcription_provider = tpEl.value;
  const gmEl = document.getElementById('pc_groq_whisper_model');
  if (gmEl) payload.groq_whisper_model = gmEl.value;

  showGlobalLoader('Saving Pipeline Settings...', 'Applying configuration to all components.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/pipeline-config`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok || !j.success) throw new Error(j.detail || 'Failed to save');
    showToast(j.message || 'Pipeline settings saved!', 'success');
    // Reload to reflect saved values
    loadPipelineConfig();
  } catch (err) {
    hideGlobalLoader();
    showToast(err.message || 'Save failed', 'error');
  }
}

async function saveTranscriptionEngine() {
  const payload = {};
  const tpEl = document.getElementById('pc_transcription_provider');
  const gmEl = document.getElementById('pc_groq_whisper_model');
  const fwEl = document.getElementById('pc_faster_whisper_model');
  if (tpEl) payload.transcription_provider = tpEl.value;
  if (gmEl) payload.groq_whisper_model = gmEl.value;
  if (fwEl) payload.faster_whisper_model = fwEl.value;

  showGlobalLoader('Saving Transcription Engine...', 'Applying provider configuration.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/pipeline-config`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok || !j.success) throw new Error(j.detail || 'Failed to save');
    showToast('Transcription engine saved!', 'success');
    loadPipelineConfig();
  } catch (err) {
    hideGlobalLoader();
    showToast(err.message || 'Save failed', 'error');
  }
}

function toggleTranscriptionOptions() {
  const provider = document.getElementById('pc_transcription_provider')?.value || 'groq';
  const groqWrap = document.getElementById('groqModelWrap');
  const fwWrap = document.getElementById('fasterWhisperModelWrap');
  if (groqWrap) groqWrap.style.display = provider === 'groq' ? '' : 'none';
  if (fwWrap) fwWrap.style.display = provider === 'faster_whisper' ? '' : 'none';
}

const _SCENE_META = {
  local: { name: 'Local — Wan2.1 / LTX-Video' },
  fal: { name: 'fal.ai' },
  replicate: { name: 'Replicate' },
};
const _SCENE_KEY_ENDPOINTS = {
  fal: 'https://queue.fal.run',
  replicate: 'https://api.replicate.com/v1',
};

async function loadSceneProviders() {
  try {
    const r = await fetch(`${API_BASE}/api/admin/scene-providers`, { headers: hdr() });
    if (!r.ok) return;
    const j = await r.json();
    const providers = j.providers || [];
    window._sceneProviders = providers;
    _populateSceneFields(providers);
    renderSavedProviders(providers);
    // Also update Config tab's Video Engines summary card (live, not hardcoded)
    const vcEl = document.getElementById('videoCardSceneSummary');
    if (vcEl) {
      const active = providers.find(p => p.is_active);
      if (active) vcEl.innerHTML = `Active: <strong style="color:#fbbf24;">${escapeHtml(active.name)}</strong> (${escapeHtml(active.provider_key)})`;
      else vcEl.innerHTML = `<span style="color:#fbbf24;">⚠️ No scene provider active — fallback to image/template.</span>`;
    }
  } catch (err) {
    console.error('Failed to load scene providers:', err);
  }
}

async function refreshConfigSummaries() {
  // Refresh Config tab's live summaries (not hardcoded)
  try {
    const pr = await fetch(`${API_BASE}/api/admin/pipeline-config`, { headers: hdr() });
    const pj = await pr.json();
    const prov = (pj.config && pj.config.transcription_provider) || 'faster_whisper';
    const te = document.getElementById('transActiveSummary');
    const tb = document.getElementById('transActiveBadge');
    if (te) te.innerHTML = `Active: <strong style="color:#22d3ee;">${escapeHtml(prov)}</strong>`;
    if (tb) tb.textContent = prov;
  } catch {}
  loadSceneProviders();
}

async function refreshVideoCardScene() {
  showGlobalLoader('Refreshing scene providers...', '');
  await loadSceneProviders();
  hideGlobalLoader();
  showToast('Scene providers refreshed', 'success');
}

function _populateSceneFields(providers) {
  const select = document.getElementById('pc_scene_provider');
  const active = providers.find(p => p.is_active);
  if (select && active) select.value = active.provider_key;

  const map = { local: {}, fal: {}, replicate: {} };
  for (const p of providers) map[p.provider_key] = p;

  // Local
  const le = document.getElementById('pc_local_endpoint');
  if (le && map.local && map.local.endpoint) le.value = map.local.endpoint;
  const lm = document.getElementById('pc_local_model');
  if (lm && map.local && map.local.model_name) lm.value = map.local.model_name;
  const lt = document.getElementById('pc_local_timeout');
  if (lt && map.local && map.local.timeout_seconds) lt.value = map.local.timeout_seconds;
  const cuEl = document.getElementById('pc_comfyui_url');
  if (cuEl && map.local && map.local.endpoint) cuEl.value = map.local.endpoint;

  // fal
  const fe = document.getElementById('pc_fal_api_key');
  if (fe) { fe.value = ''; fe.placeholder = map.fal && map.fal.api_key_masked ? `•••••••• saved key — ${map.fal.api_key_masked}` : 'fal AI API key'; }
  const fm = document.getElementById('pc_fal_model');
  if (fm && map.fal && map.fal.model_name) fm.value = map.fal.model_name;
  const ft = document.getElementById('pc_fal_timeout');
  if (ft && map.fal && map.fal.timeout_seconds) ft.value = map.fal.timeout_seconds;

  // replicate
  const re = document.getElementById('pc_replicate_api_key');
  if (re) { re.value = ''; re.placeholder = map.replicate && map.replicate.api_key_masked ? `•••••••• saved token — ${map.replicate.api_key_masked}` : 'r8_... Replicate API token'; }
  const rm = document.getElementById('pc_replicate_model');
  if (rm && map.replicate && map.replicate.model_name) rm.value = map.replicate.model_name;
  const rt = document.getElementById('pc_replicate_timeout');
  if (rt && map.replicate && map.replicate.timeout_seconds) rt.value = map.replicate.timeout_seconds;
}

async function saveSceneGeneration() {
  const provider = document.getElementById('pc_scene_provider')?.value || 'local';

  // We save the provider's config (key/model/endpoint/timeout), then activate it.
  const payload = { provider_key: provider, is_active: true, model_name: '', endpoint: '', timeout_seconds: 180 };
  if (provider === 'local') {
    const le = document.getElementById('pc_local_endpoint'); if (le && le.value.trim()) payload.endpoint = le.value.trim();
    const lm = document.getElementById('pc_local_model'); if (lm && lm.value.trim()) payload.model_name = lm.value.trim();
    const lt = document.getElementById('pc_local_timeout'); if (lt && lt.value) payload.timeout_seconds = parseInt(lt.value) || 700;
    // persist comfyui_url so other code paths keep working
    if (payload.endpoint) {
      try { await fetch(`${API_BASE}/api/admin/pipeline-config`, { method: 'POST', headers: hdr(), body: JSON.stringify({ comfyui_url: payload.endpoint }) }); } catch (_e) { }
    }
  } else {
    const keyEl = document.getElementById(`pc_${provider}_api_key`);
    if (keyEl && keyEl.value.trim()) payload.api_key = keyEl.value.trim();
    const mEl = document.getElementById(`pc_${provider}_model`);
    if (mEl && mEl.value.trim()) payload.model_name = mEl.value.trim();
    const tEl = document.getElementById(`pc_${provider}_timeout`);
    if (tEl && tEl.value) payload.timeout_seconds = parseInt(tEl.value) || 180;
  }

  showGlobalLoader('Saving Scene Generation...', 'Saving provider config and activating it.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/scene-providers`, {
      method: 'POST', headers: hdr(), body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!r.ok || !j.success) throw new Error(j.detail || 'Failed to save');
    // Activate (redundant if is_active already true, but enforces key validation)
    const ar = await fetch(`${API_BASE}/api/admin/scene-providers/${provider}/activate`, { method: 'POST', headers: hdr(), body: '{}' });
    const aj = await ar.json();
    if (!ar.ok) throw new Error(aj.detail || 'Failed to activate provider');
    hideGlobalLoader();
    showToast(`Scene generation saved & activated (${_SCENE_META[provider].name})`, 'success');
    // Clear key inputs (they are stored securely)
    if (provider !== 'local') { const k = document.getElementById(`pc_${provider}_api_key`); if (k) k.value = ''; }
    loadSceneProviders();
  } catch (err) {
    hideGlobalLoader();
    showToast(err.message || 'Save failed', 'error');
  }
}

async function activateSceneProvider(provider) {
  showGlobalLoader('Activating provider...', 'Setting the active scene generation provider.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/scene-providers/${provider}/activate`, { method: 'POST', headers: hdr(), body: '{}' });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed to activate');
    showToast(j.message, 'success');
    loadSceneProviders();
  } catch (err) {
    hideGlobalLoader();
    showToast(err.message || 'Activation failed', 'error');
  }
}

function toggleSceneGenOptions() {
  const provider = document.getElementById('pc_scene_provider')?.value || 'local';
  const localWrap = document.getElementById('localProvWrap');
  const falWrap = document.getElementById('falProvWrap');
  const repWrap = document.getElementById('replicateProvWrap');
  if (localWrap) localWrap.style.display = provider === 'local' ? '' : 'none';
  if (falWrap) falWrap.style.display = provider === 'fal' ? '' : 'none';
  if (repWrap) repWrap.style.display = provider === 'replicate' ? '' : 'none';
}


function _comfyPanel(line, level) {
  const panel = document.getElementById('comfy_setup_panel');
  if (!panel) return;
  if (line === null) { panel.style.display = 'none'; panel.textContent = ''; return; }
  panel.style.display = 'block';
  const stamp = new Date().toLocaleTimeString();
  const tag = level === 'err' ? '✖' : level === 'warn' ? '⚠' : level === 'ok' ? '✔' : '·';
  const color = level === 'err' ? '#fca5a5' : level === 'warn' ? '#fde68a' : level === 'ok' ? '#86efac' : '#cbd5e1';
  panel.innerHTML += `<div style="color:${color}; margin:1px 0;">[${stamp}] ${tag} ${escapeHtml(String(line))}</div>`;
  panel.scrollTop = panel.scrollHeight;
}

function _comfyPanelReset() {
  const panel = document.getElementById('comfy_setup_panel');
  if (panel) { panel.style.display = 'block'; panel.innerHTML = ''; }
}

async function refreshComfyStatus() {
  try {
    const r = await fetch(`${API_BASE}/api/admin/comfyui/status`, { headers: hdr() });
    if (!r.ok) return null;
    return await r.json();
  } catch (_e) {
    return null;
  }
}

async function setupAndStartComfyUI() {
  // 1. Quick status check — if everything is already installed AND running, just confirm.
  const status = await refreshComfyStatus();
  if (status && status.ready_to_generate) {
    showToast('Local CogVideoX-2b is already installed and ComfyUI is running. Nothing to do.', 'success');
    await checkComfyUIModels();
    return;
  }

  // 2. If everything is installed but ComfyUI is not running, just start it.
  if (status && status.all_ready && !status.comfyui_running) {
    _comfyPanel('All components installed — starting ComfyUI…', 'ok');
    await startComfyUI();
    _comfyPanel(null);
    return;
  }

  // 3. Otherwise: run the full setup (clone + venv + PyTorch + custom node + models)
  //    then auto-start ComfyUI in the same call. Long-running — keep the panel open.
  const btn = document.getElementById('btn_comfy_setup_start');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Installing CogVideoX-2B…'; }

  _comfyPanelReset();
  if (status) {
    _comfyPanel('Checking what is already installed…', 'info');
    for (const c of (status.components || [])) {
      _comfyPanel(`${c.present ? '✔' : '✖'} ${c.label}${c.size_mb ? ' — ' + c.size_mb + ' MB' : ''}`,
        c.present ? 'ok' : (c.optional ? 'warn' : 'err'));
    }
    const missing = (status.components || []).filter(c => !c.present && !c.optional);
    if (missing.length) {
      _comfyPanel(`Will install: ${missing.map(m => m.label).join(', ')}`, 'warn');
    }
  } else {
    _comfyPanel('Could not query current install status — proceeding with full setup.', 'warn');
  }

  showGlobalLoader('Setting up Local CogVideoX-2B…',
    'This installs ComfyUI + Python venv + PyTorch CUDA + the CogVideoXWrapper custom node + downloads ~9 GB of model files. It can take 10-30 minutes on a fresh machine. Watch the live log below for progress.');

  try {
    const r = await fetch(`${API_BASE}/api/admin/comfyui/setup`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ start_after: true })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (j.actions) {
      for (const a of j.actions) {
        _comfyPanel(`${a.ok ? '✔' : '✖'} ${a.label}${a.detail ? ' — ' + a.detail : ''}`,
          a.ok ? 'ok' : 'err');
      }
    }
    if (j.log_tail) {
      const tailLines = j.log_tail.split('\n').slice(-20);
      _comfyPanel('── last 20 log lines ──', 'info');
      for (const ln of tailLines) if (ln.trim()) _comfyPanel(ln, 'info');
    }
    if (j.start_result) {
      showToast(j.start_result.message || 'ComfyUI start attempted', j.start_result.running ? 'success' : 'warning');
    } else {
      showToast(j.message || 'Setup finished', j.all_ready ? 'success' : 'warning');
    }
    // Final verification
    await checkComfyUIModels();
  } catch (e) {
    hideGlobalLoader();
    _comfyPanel('Network / server error: ' + e.message, 'err');
    showToast('Setup failed: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔧 Setup & Start Local CogVideoX-2B'; }
  }
}

async function startComfyUI() {
  showGlobalLoader('Starting / Checking Local ComfyUI...', 'Launching CogVideoX server and probing http://127.0.0.1:8188. First launch can take 60-120s.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/comfyui/start`, {
      method: 'POST',
      headers: hdr(),
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok || !j.success) throw new Error(j.detail || 'Failed to start ComfyUI');
    showToast(j.message, j.running ? 'success' : 'warning');
    if (j.log_tail) {
      console.log('comfyui.log tail:\n' + j.log_tail);
      showToast('ComfyUI log: ' + j.log_tail.split('\n').slice(-3).join(' '), 'error');
    }
    // Always check models — even if ComfyUI was already running, we need to verify GPU + CogVideo detection
    await checkComfyUIModels();
  } catch (e) {
    hideGlobalLoader();
    showToast('Start failed: ' + e.message, 'error');
  }
}

async function checkComfyUIModels() {
  showGlobalLoader('Checking ComfyUI GPU & CogVideoX Model...', 'Probing ComfyUI for GPU device and model availability.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/video-provider/test`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ provider: 'local', value: '' })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (j.message) showToast(j.message, j.verified ? 'success' : 'warning');
  } catch (_e) {
    hideGlobalLoader();
  }
}

async function testVideoProvider(provider, inputId) {
  if (provider === 'local') {
    checkComfyUIModels();
    return;
  }
  const value = inputId ? (document.getElementById(inputId)?.value.trim() || '') : '';
  showGlobalLoader(`Testing ${provider} connection...`, 'Contacting provider to verify API authentication.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/video-provider/test`, {
      method: 'POST',
      headers: hdr(),
      body: JSON.stringify({ provider, value })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Test failed');
    showToast(j.message, j.verified ? 'success' : 'error');
  } catch (e) {
    hideGlobalLoader();
    showToast('Test failed: ' + e.message, 'error');
  }
}

async function testSceneProvider(provider) {
  // Test using the saved provider config (no typed key) — falls back to stored key server-side.
  if (provider === 'local') { checkComfyUIModels(); return; }
  showGlobalLoader(`Testing ${_SCENE_META[provider].name}...`, 'Contacting provider to verify API authentication.');
  try {
    const r = await fetch(`${API_BASE}/api/admin/video-provider/test`, {
      method: 'POST', headers: hdr(), body: JSON.stringify({ provider, value: '' })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Test failed');
    showToast(j.message, j.verified ? 'success' : 'error');
  } catch (e) {
    hideGlobalLoader();
    showToast('Test failed: ' + e.message, 'error');
  }
}

function renderSavedProviders(providers) {
  const display = document.getElementById('savedProvidersDisplay');
  const list = document.getElementById('savedProvidersList');
  const statusBanner = document.getElementById('sceneGenActiveStatus');
  if (!display || !list) return;

  const active = providers.find(p => p.is_active);
  const hasAny = providers.some(p => p.api_key_masked || p.provider_key === 'local');

  // Update status banner
  if (statusBanner) {
    if (active) {
      statusBanner.style.display = 'block';
      statusBanner.style.background = 'rgba(168,85,247,0.12)';
      statusBanner.style.borderColor = 'rgba(168,85,247,0.35)';
      statusBanner.style.color = '#e9d5ff';
      const extra = active.provider_key === 'local' ? ' (offline GPU)' : '';
      statusBanner.innerHTML = `✓ Active Scene Video Provider: <strong>${escapeHtml(active.name || active.provider_key)}</strong>${extra}. Script-to-Video Shorts will use this active provider.`;
    } else if (hasAny) {
      statusBanner.style.display = 'block';
      statusBanner.style.background = 'rgba(251,191,36,0.1)';
      statusBanner.style.borderColor = 'rgba(251,191,36,0.3)';
      statusBanner.style.color = '#fbbf24';
      statusBanner.innerHTML = '⚠ No provider is active. Choose one above and click <strong>Save &amp; Activate Provider</strong>.';
    } else {
      statusBanner.style.display = 'none';
    }
  }

  if (!providers.length) {
    display.style.display = 'none';
    return;
  }

  display.style.display = 'block';
  list.innerHTML = providers.map(p => {
    const activeBadge = p.is_active ? ' <span style="color:#c084fc; font-weight:800; font-size:0.72rem; padding:3px 10px; background:rgba(168,85,247,0.2); border:1px solid rgba(168,85,247,0.4); border-radius:9999px;">● ACTIVE</span>' : '';
    const masked = p.api_key_masked ? maskKey(p.api_key_masked) : (p.provider_key === 'local' ? 'http://127.0.0.1:8188' : '—');
    const nameStr = p.name || p.provider_key;
    return `
      <div style="display:flex; align-items:center; justify-content:space-between; gap:1.25rem; padding:0.9rem 1.25rem; background:rgba(255,255,255,0.02); border-radius:10px; border:1px solid rgba(255,255,255,0.07); flex-wrap:wrap;">
        <div style="display:flex; align-items:center; gap:0.75rem; min-width:220px;">
          <span style="font-size:0.88rem; color:var(--text-primary); font-weight:700;">${escapeHtml(nameStr)}</span>
          ${activeBadge}
        </div>
        <div style="font-size:0.8rem; color:var(--text-muted); flex:1; min-width:200px; display:flex; gap:1.25rem; align-items:center; flex-wrap:wrap;">
          <span>Model: <code style="color:#e2e8f0; background:rgba(255,255,255,0.05); padding:2px 6px; border-radius:4px;">${escapeHtml(p.model_name || 'default')}</code></span>
          <span>Key / Endpoint: <span style="font-family:monospace; color:#94a3b8;">${escapeHtml(masked)}</span></span>
        </div>
        <div style="display:flex; gap:0.5rem; align-items:center;">
          <button class="btn-outline btn-sm" style="height:38px; padding:0 0.95rem; font-size:0.8rem;" onclick="editSceneProvider('${p.provider_key}')">✏️ Edit</button>
          <button class="btn-primary btn-sm" style="height:38px; padding:0 0.95rem; font-size:0.8rem;" onclick="activateSceneProvider('${p.provider_key}')">✅ Activate</button>
          ${p.provider_key !== 'local' ? `<button class="btn-outline btn-sm btn-danger-outline" style="height:38px; padding:0 0.95rem; font-size:0.8rem;" onclick="clearSceneProviderKey('${p.provider_key}')">🗑️ Clear Key</button>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function formatTemplateColorSwatches(colorsObj) {
  if (!colorsObj || typeof colorsObj !== 'object') return '<span style="color:var(--text-muted); font-size:0.75rem;">Default</span>';
  return Object.entries(colorsObj).map(([role, val]) => {
    let cssColor = String(val).trim();
    if (cssColor.includes(',') && !cssColor.startsWith('rgb')) {
      cssColor = `rgb(${cssColor})`;
    }
    const roleLabel = role.charAt(0).toUpperCase() + role.slice(1);
    return `
      <span style="display:inline-flex; align-items:center; gap:0.35rem; background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.08); padding:3px 8px; border-radius:6px; font-size:0.72rem;" title="${escapeHtml(roleLabel)}: ${escapeHtml(String(val))}">
        <span style="width:10px; height:10px; border-radius:50%; background:${escapeHtml(cssColor)}; display:inline-block; border:1px solid rgba(255,255,255,0.3); box-shadow:0 0 6px ${escapeHtml(cssColor)}; flex-shrink:0;"></span>
        <span style="color:var(--text-muted);">${escapeHtml(roleLabel)}</span>
      </span>
    `;
  }).join(' ');
}

async function loadTemplateBackgrounds() {
  const statusEl = document.getElementById('templateBgStatus');
  const gridEl = document.getElementById('templateBgGrid');
  if (!gridEl) return;
  try {
    const r = await fetch(`${API_BASE}/api/admin/template-backgrounds`, { headers: hdr() });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || 'Failed to load templates');
    const tpls = j.templates || [];
    if (statusEl) {
      statusEl.style.display = '';
      if (tpls.length === 0) {
        statusEl.style.background = 'rgba(251,191,36,0.1)'; statusEl.style.borderColor = 'rgba(251,191,36,0.3)'; statusEl.color = '#fbbf24';
        statusEl.innerHTML = '⚠️ No templates found in <code>assets/templates/backgrounds/</code>. Tier 3 will use solid-color fallback.';
      } else {
        const enabled = tpls.filter(t => t.enabled).length;
        statusEl.style.background = 'rgba(168,85,247,0.12)'; statusEl.style.borderColor = 'rgba(168,85,247,0.35)'; statusEl.style.color = '#e9d5ff';
        statusEl.innerHTML = `✅ <strong>${tpls.length} templates discovered</strong> — <strong>${enabled} enabled</strong> for Tier 3 fallback round-robin selection.`;
      }
    }
    if (tpls.length === 0) { gridEl.innerHTML = '<p style="color:var(--text-muted); font-size:0.85rem;">No templates discovered.</p>'; return; }
    gridEl.innerHTML = tpls.map(t => {
      const safeStr = `${t.caption_safe_zone?.y_min_pct ?? 0.6}–${t.caption_safe_zone?.y_max_pct ?? 0.9}`;
      return `
        <div style="background:rgba(255,255,255,0.02); border:1px solid ${t.enabled ? 'rgba(168,85,247,0.4)' : 'rgba(255,255,255,0.07)'}; border-radius:12px; padding:1.1rem; display:flex; flex-direction:column; gap:0.75rem; box-shadow:0 4px 20px rgba(0,0,0,0.25); transition:all 0.25s ease;">
          <div style="display:flex; align-items:center; justify-content:space-between; gap:0.5rem; flex-wrap:wrap;">
            <span style="font-weight:800; color:var(--text-primary); font-size:0.95rem;">${escapeHtml(t.name)}</span>
            <label style="display:inline-flex; align-items:center; gap:0.4rem; cursor:pointer; font-size:0.78rem; font-weight:700; color:${t.enabled ? '#e9d5ff' : 'var(--text-muted)'}; background:${t.enabled ? 'rgba(168,85,247,0.2)' : 'rgba(255,255,255,0.04)'}; border:1px solid ${t.enabled ? 'rgba(168,85,247,0.45)' : 'rgba(255,255,255,0.08)'}; padding:4px 10px; border-radius:9999px;">
              <input type="checkbox" ${t.enabled ? 'checked' : ''} onchange="toggleTemplateEnabled('${t.template_id}', this.checked)" style="width:15px; height:15px; accent-color:#a855f7; cursor:pointer;" />
              ${t.enabled ? 'Enabled' : 'Disabled'}
            </label>
          </div>
          <p style="font-size:0.8rem; color:var(--text-muted); margin:0; line-height:1.45;">${escapeHtml(t.description || '')}</p>
          
          <div style="display:flex; gap:0.5rem; flex-wrap:wrap; font-size:0.74rem;">
            <span style="background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.08); padding:3px 8px; border-radius:6px; color:var(--text-muted);">⏱️ ${t.loop_duration}s loop</span>
            <span style="background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.08); padding:3px 8px; border-radius:6px; color:var(--text-muted);">📐 safe: ${safeStr}</span>
            <span style="background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.08); padding:3px 8px; border-radius:6px; color:var(--text-muted); font-family:monospace;">🆔 ${escapeHtml(t.template_id)}</span>
          </div>

          <div>
            <div style="font-size:0.72rem; color:var(--text-muted); margin-bottom:0.35rem; font-weight:700;">Suggested Palette:</div>
            <div style="display:flex; gap:0.4rem; flex-wrap:wrap;">
              ${formatTemplateColorSwatches(t.suggested_colors)}
            </div>
          </div>

          <div style="position:relative; overflow:hidden; border-radius:10px; border:1px solid rgba(255,255,255,0.1); background:#05060b;">
            <video src="/assets/templates/backgrounds/${encodeURIComponent(t.template_id)}/background.mp4" autoplay loop muted playsinline style="width:100%; aspect-ratio:9/16; object-fit:cover; display:block; max-height:240px;"></video>
          </div>
        </div>
      `;
    }).join('');
  } catch (e) {
    if (gridEl) gridEl.innerHTML = `<p style="color:#fca5a5; font-size:0.85rem;">Failed to load templates: ${escapeHtml(e.message)}</p>`;
  }
}

async function toggleTemplateEnabled(templateId, enabled) {
  try {
    const cur = await (await fetch(`${API_BASE}/api/admin/template-backgrounds`, { headers: hdr() })).json();
    const allIds = (cur.templates || []).map(t => t.template_id);
    const enabledIds = (cur.templates || []).filter(t => t.enabled).map(t => t.template_id);
    let nextIds;
    if (enabled) { nextIds = [...new Set([...enabledIds, templateId])]; }
    else { nextIds = enabledIds.filter(id => id !== templateId); }
    // If all would be enabled, send null (= all enabled, clears DB override)
    const payload = (nextIds.length === allIds.length || nextIds.length === 0) ? { enabled_ids: null } : { enabled_ids: nextIds };
    showGlobalLoader('Updating templates...', `Toggling ${templateId}`);
    const r = await fetch(`${API_BASE}/api/admin/template-backgrounds/enabled`, { method: 'POST', headers: hdr(), body: JSON.stringify(payload) });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed');
    showToast(j.message, 'success');
    loadTemplateBackgrounds();
  } catch (e) { hideGlobalLoader(); showToast(e.message, 'error'); }
}

async function setAllTemplatesEnabled(enabled) {
  const body = enabled ? { enabled_ids: null } : { enabled_ids: ["__none__"] };
  try {
    showGlobalLoader(enabled ? 'Enabling all templates...' : 'Disabling templates...', '');
    const r = await fetch(`${API_BASE}/api/admin/template-backgrounds/enabled`, { method: 'POST', headers: hdr(), body: JSON.stringify(body) });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed');
    showToast(j.message, 'success');
    loadTemplateBackgrounds();
  } catch (e) { hideGlobalLoader(); showToast(e.message, 'error'); }
}

function maskKey(k) {
  if (!k) return '';
  if (k.includes('****')) return k; // already a masked form from the server
  const s = String(k);
  if (s.length <= 12) return s.slice(0, 4) + '********' + s.slice(-2);
  return s.slice(0, 8) + '********' + s.slice(-4);
}

function editSceneProvider(providerKey) {
  const select = document.getElementById('pc_scene_provider');
  if (select) {
    select.value = providerKey;
    toggleSceneGenOptions();
  }
  if (providerKey !== 'local') {
    const keyInput = document.getElementById(`pc_${providerKey}_api_key`);
    if (keyInput) { keyInput.focus(); keyInput.select(); }
  }
}

async function clearSceneProviderKey(providerKey) {
  const meta = _SCENE_META[providerKey] || { name: providerKey };
  showConfirmModal({
    title: `Clear ${meta.name} API Key`,
    message: `This removes the stored key for ${meta.name}. You must add a new key before it can be used again.`,
    icon: '🗑️',
    confirmText: 'Clear Key',
    cancelText: 'Keep Key',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader(`Clearing ${meta.name} key...`, 'Removing saved API key from database.');
      try {
        const r = await fetch(`${API_BASE}/api/admin/scene-providers/${providerKey}/clear-key`, {
          method: 'POST', headers: hdr(), body: '{}',
        });
        const j = await r.json();
        hideGlobalLoader();
        if (!r.ok) throw new Error(j.detail || 'Failed to clear key');
        showToast(`${meta.name} key cleared`, 'success');
        await loadSceneProviders();
      } catch (err) {
        hideGlobalLoader();
        showToast(err.message || 'Failed to clear key', 'error');
      }
    }
  });
}

async function resetPipelineConfig() {
  showConfirmModal({
    title: 'Reset ALL Pipeline Settings',
    message: 'Reset ALL pipeline settings to defaults? This cannot be undone.',
    icon: '⚠️',
    confirmText: 'Yes, Reset',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Resetting...', 'Restoring hardcoded defaults.');
      try {
        const r = await fetch(`${API_BASE}/api/admin/pipeline-config/reset`, {
          method: 'POST',
          headers: hdr(),
        });
        const j = await r.json();
        hideGlobalLoader();
        if (!r.ok || !j.success) throw new Error(j.detail || 'Reset failed');
        showToast(j.message || 'Pipeline settings reset!', 'success');
        loadPipelineConfig();
      } catch (err) {
        hideGlobalLoader();
        showToast(err.message || 'Reset failed', 'error');
      }
    }
  });
}


/* ─── Initialization ─────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', async () => {
  if (await checkAdmin()) {
    loadAll();
  }
});
