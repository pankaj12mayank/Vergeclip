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

function getApiBase() {
  const s = localStorage.getItem('CUSTOM_API_BASE');
  if (s) return s;
  if (window.location.origin && window.location.origin !== 'null' && window.location.protocol !== 'file:') {
    if (window.location.port === '5000') {
      return window.location.origin;
    }
    if (!window.location.port && !['localhost', '127.0.0.1'].includes(window.location.hostname)) {
      return window.location.origin;
    }
  }
  return 'http://localhost:5000';
}

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

function escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
    if (!keysDiv) return;
    keysDiv.innerHTML = '';

    const keys = ['VIDEOSAILOR_API_KEY', 'ASSEMBLYAI_API_KEY', 'GROQ_API_KEY'];
    const keyInfo = {
      VIDEOSAILOR_API_KEY: { title: '🎙️ VideoSailor Engine (Optional)', desc: 'Cloud video downloader. Without this key, pipeline uses yt-dlp (FREE, local). Only add if you have a paid VideoSailor subscription.' },
      ASSEMBLYAI_API_KEY: { title: '💬 AssemblyAI Speech Engine', desc: 'Cloud transcription with word-level timestamps and multi-speaker diarization ($0.15-$0.21/hr).' },
      GROQ_API_KEY: { title: '🆓 Groq Whisper Engine (FREE)', desc: 'FREE transcription via Groq Whisper — 8 hrs audio/day, no card needed. Use alongside or instead of AssemblyAI.' }
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
        <div style="position:relative; display:flex; align-items:center;">
          <input class="admin-input" id="cfg_${k}" type="password" placeholder="Enter key (leave empty to test configured)" style="padding-right:2.5rem;" />
          <button type="button" class="input-icon-btn" onclick="togglePasswordVisibility('cfg_${k}', this)" title="Show/Hide Key" style="right:10px;">👁️</button>
        </div>
        <div class="core-key-actions">
          <button class="btn-outline btn-sm" onclick="testKey('${k}')" title="Test Live Connection">⚡ Verify Connection</button>
          <button class="btn-primary btn-sm" onclick="saveKey('${k}')">💾 Save</button>
        </div>
      `;
      keysDiv.appendChild(card);
    });

    // Load custom AI providers from DB
    loadCustomProviders(keysDiv, setKeysCount, keys.length);

    const kpiKeys = document.getElementById('kpi-keys');
    if (kpiKeys) kpiKeys.textContent = `${setKeysCount} / ${keys.length}`;

    // Limits & Storage
    const lim = document.getElementById('configLimits');
    if (lim) {
      lim.innerHTML = `
        <div class="admin-card" style="padding:1.25rem;">
          <div style="font-weight:800; font-size:0.9rem; margin-bottom:0.25rem;">FREE_TIER_MONTHLY_LIMIT</div>
          <p style="font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem;">Default shorts allowance per registered free account/month.</p>
          <input class="admin-input" id="cfg_FREE_TIER_MONTHLY_LIMIT" placeholder="5" />
          <button class="btn-primary btn-sm" style="margin-top:0.75rem; width:100%;" onclick="saveKey('FREE_TIER_MONTHLY_LIMIT')">Save Limit</button>
        </div>
        <div class="admin-card" style="padding:1.25rem;">
          <div style="font-weight:800; font-size:0.9rem; margin-bottom:0.25rem;">MAX_VIDEO_DURATION_MINUTES</div>
          <p style="font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem;">Ceiling for input YouTube video length.</p>
          <input class="admin-input" id="cfg_MAX_VIDEO_DURATION_MINUTES" placeholder="90" />
          <button class="btn-primary btn-sm" style="margin-top:0.75rem; width:100%;" onclick="saveKey('MAX_VIDEO_DURATION_MINUTES')">Save Duration</button>
        </div>
        <div class="admin-card" style="padding:1.25rem;">
          <div style="font-weight:800; font-size:0.9rem; margin-bottom:0.25rem;">STORAGE_PATH</div>
          <p style="font-size:0.8rem; color:var(--text-muted); margin-bottom:0.75rem;">Local folder for storing generated clips.</p>
          <input class="admin-input" id="cfg_STORAGE_PATH" placeholder="./storage" />
          <button class="btn-primary btn-sm" style="margin-top:0.75rem; width:100%;" onclick="saveKey('STORAGE_PATH')">Save Path</button>
        </div>
      `;
      // Populate current values from config
      const limEl = document.getElementById('cfg_FREE_TIER_MONTHLY_LIMIT');
      const durEl = document.getElementById('cfg_MAX_VIDEO_DURATION_MINUTES');
      const storEl = document.getElementById('cfg_STORAGE_PATH');
      if (limEl) limEl.value = j.config.free_tier_monthly_limit || '5';
      if (durEl) durEl.value = j.config.max_video_duration_minutes || '90';
      if (storEl) storEl.value = j.config.storage_path || './storage';
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
  if (!val) return showToast('Please enter a valid value to save.', 'warning');

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

        <div style="position:relative; display:flex; align-items:center;">
          <input class="admin-input" id="cp_key_${p.id}" type="password" placeholder="Enter key (leave empty to test configured)" style="padding-right:2.5rem;" />
          <button type="button" class="input-icon-btn" onclick="togglePasswordVisibility('cp_key_${p.id}', this)" title="Show/Hide Key" style="right:10px;">👁️</button>
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

    prompts.forEach(p => {
      const card = document.createElement('div');
      card.className = 'admin-card';
      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.75rem;">
          <div style="display:flex; align-items:center; gap:0.6rem;">
            <strong style="font-size:1.05rem; color:#fff;">${escapeHtml(p.name)} <span style="color:var(--text-muted); font-size:0.85rem;">(${escapeHtml(p.version)})</span></strong>
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
          <span class="badge ${isOwner ? 'badge-purple' : 'badge-green'}">${isOwner ? 'Owner' : (u.tier || 'free')}</span>
        </td>
        <td style="color:var(--text-muted); font-size:0.82rem;">${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
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
          <td colspan="7" style="text-align:center; padding:2rem; color:var(--text-muted);">
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
          <td style="font-family:var(--font-mono); font-weight:700; color:#fff;">${jb.id.slice(0, 8)}</td>
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
          <td style="color:var(--text-muted); font-size:0.8rem;">${jb.created_at ? new Date(jb.created_at).toLocaleTimeString() : '—'}</td>
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
      const timeStr = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '—';
      const hasPayload = Boolean(ev.request_data || ev.response_data);

      tr.innerHTML = `
        <td><input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleSelectAudit('${ev.id}', this.checked)" /></td>
        <td style="color:var(--text-muted); font-family:var(--font-mono); font-size:0.82rem;">${timeStr}</td>
        <td><span class="badge ${sevClass}">${ev.severity}</span></td>
        <td><span class="badge" style="background:rgba(255,255,255,0.06);">${escapeHtml(ev.category)}</span></td>
        <td><strong style="font-size:0.88rem; color:#fff;">${escapeHtml(ev.action)}</strong></td>
        <td style="color:var(--text-main); font-size:0.85rem; max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(ev.detail)}">${escapeHtml(ev.detail)}</td>
        <td style="color:var(--text-muted); font-size:0.82rem;">${escapeHtml(ev.user_id)} (${escapeHtml(ev.ip)})</td>
        <td style="text-align:right;">
          <button class="btn-outline btn-sm" onclick="openAuditModal('${ev.id}')" style="padding:0.25rem 0.5rem; font-size:0.75rem;">
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
  'target_width','target_height','target_fps','max_short_duration','min_short_duration',
  // Clip Selection
  'clip_min_duration','clip_max_duration','clip_top_n','clip_min_score','clip_min_separation',
  'clip_step_size','clip_overlap_threshold','clip_distribution_strategy',
  // Semantic Ranking
  'semantic_default_pool_size','semantic_min_score','semantic_default_top_n','semantic_default_separation',
  // Captions
  'caption_font_size','caption_max_words','caption_min_words','caption_max_lines','caption_max_width','caption_y',
  'caption_text_color','caption_highlight_color','caption_outline_color','caption_outline_width',
  'caption_start_padding','caption_end_padding','caption_max_duration','caption_min_duration',
  // Enhancement
  'auto_color_filter_enabled','auto_video_filter','auto_pitch_shift_enabled','auto_pitch_semitones',
];

const BOOL_FIELDS = ['auto_color_filter_enabled','auto_pitch_shift_enabled'];

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

    // Scoring weights
    const weights = flat.scoring_weights || {};
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
    div.innerHTML = `<label class="auth-label">${key}</label><input class="admin-input" type="number" step="0.5" data-weight-key="${key}" value="${val}" />`;
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
  if (tpEl) payload.transcription_provider = tpEl.value;
  if (gmEl) payload.groq_whisper_model = gmEl.value;

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

async function resetPipelineConfig() {
  if (!confirm('Reset ALL pipeline settings to defaults? This cannot be undone.')) return;
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


/* ─── Initialization ─────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', async () => {
  if (await checkAdmin()) {
    loadAll();
  }
});
