/* ==========================================================================
   app.js — PodcastShorts AI Generator & Gallery Client
   ========================================================================== */

function getApiBase() {
  const saved = localStorage.getItem('CUSTOM_API_BASE');
  if (saved) return saved;
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
let allRenderedClips = [];
let pollInterval = null;
let sseSource = null;

/* ─── Global scroll lock when any modal is open ─────────────────────────────── */
(function _initScrollLock() {
  function _checkScrollLock() {
    const anyOpen = document.querySelector('.video-modal-backdrop[style*="display: flex"], .video-modal-backdrop[style*="display:flex"], .video-modal-backdrop:not([style*="display: none"]):not([style*="display:none"])');
    document.body.style.overflow = anyOpen ? 'hidden' : '';
  }
  new MutationObserver(_checkScrollLock).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setTimeout(_checkScrollLock, 10); });
})();

function getAuthToken() {
  return localStorage.getItem('ps_auth_token') || '';
}

function buildVideoSrc(filename) {
  const base = `${API_BASE}/api/stream/output/${encodeURIComponent(filename)}`;
  const t = getAuthToken();
  return t ? `${base}?token=${encodeURIComponent(t)}` : base;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ─── Mode Switcher (URL vs Topic/Prompt) ─────────────────────────────────── */
window.switchGenMode = function(mode) {
  const btnUrl = document.getElementById('tabModeUrl');
  const btnTopic = document.getElementById('tabModeTopic');
  const wrapUrl = document.getElementById('modeUrlWrap');
  const wrapTopic = document.getElementById('modeTopicWrap');

  if (mode === 'url') {
    if (btnUrl) btnUrl.className = 'btn-primary btn-sm';
    if (btnTopic) btnTopic.className = 'btn-outline btn-sm';
    if (wrapUrl) wrapUrl.style.display = 'block';
    if (wrapTopic) wrapTopic.style.display = 'none';
  } else {
    if (btnUrl) btnUrl.className = 'btn-outline btn-sm';
    if (btnTopic) btnTopic.className = 'btn-primary btn-sm';
    if (wrapUrl) wrapUrl.style.display = 'none';
    if (wrapTopic) wrapTopic.style.display = 'flex';
  }
};

/* ─── Clipboard Paste Helper ─────────────────────────────────────────────── */
window.pasteFromClipboard = async function() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) {
      const input = document.getElementById('ytUrl');
      if (input) {
        input.value = text.trim();
        showToast('Pasted link from clipboard!', 'info');
      }
    }
  } catch (e) {
    showToast('Clipboard access denied. Please paste manually.', 'error');
  }
};

/* ─── Change Backend Server URL ──────────────────────────────────────────── */
window.promptBackendUrl = function() {
  const current = localStorage.getItem('CUSTOM_API_BASE') || API_BASE;
  const input = prompt('Enter your Backend API Server URL (e.g. http://localhost:5000):', current);
  if (input !== null) {
    const trimmed = input.trim().replace(/\/+$/, '');
    if (trimmed) {
      localStorage.setItem('CUSTOM_API_BASE', trimmed);
      API_BASE = trimmed;
    } else {
      localStorage.removeItem('CUSTOM_API_BASE');
      API_BASE = getApiBase();
    }
    checkBackendHealth();
    refreshOutputs();
    showToast(`Backend set to: ${API_BASE}`, 'info');
  }
};

/* ─── DOM Initialization ─────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  checkBackendHealth();
  refreshOutputs();

  const urlParams = new URLSearchParams(window.location.search);
  const passedUrl = urlParams.get('url');
  if (passedUrl) {
    const input = document.getElementById('ytUrl');
    if (input) input.value = decodeURIComponent(passedUrl);
  }

  setInterval(checkBackendHealth, 8000);

  // Navbar scroll blur effect
  window.addEventListener('scroll', () => {
    const nav = document.querySelector('.app-nav');
    if (nav) {
      if (window.scrollY > 20) nav.classList.add('scrolled');
      else nav.classList.remove('scrolled');
    }
  });
});

/* ─── Backend Health Ping ────────────────────────────────────────────────── */
async function checkBackendHealth() {
  const dot = document.getElementById('serverDot');
  const lbl = document.getElementById('serverLbl');
  const t = getAuthToken();
  const headers = t ? { 'Authorization': `Bearer ${t}` } : {};
  const endpointsToTry = [`${API_BASE}/api/status`, 'http://localhost:5000/api/status', 'http://127.0.0.1:5000/api/status'];

  for (const ep of endpointsToTry) {
    try {
      const res = await fetch(ep, { cache: 'no-store', headers });
      if (res.ok) {
        if (dot) dot.className = 'status-dot green';
        if (lbl) lbl.textContent = 'Backend Connected';
        API_BASE = ep.replace('/api/status', '');
        return;
      }
    } catch (e) {}
  }
  if (dot) dot.className = 'status-dot red';
  if (lbl) lbl.textContent = 'Backend Offline';
}

/* ─── Start Auto-Generate Pipeline ───────────────────────────────────────── */
window.startAutoGenerate = async function() {
  const url = document.getElementById('ytUrl')?.value.trim();
  if (!url) {
    showToast('Please paste a YouTube link to generate shorts.', 'error');
    document.getElementById('ytUrl')?.focus();
    return;
  }

  // Auth requirement check
  const token = getAuthToken();
  if (!token) {
    try {
      const chk = await fetch(`${API_BASE}/api/auth/check`, { headers: token ? { 'Authorization': `Bearer ${token}` } : {} }).then(r => r.json());
      if (chk.auth_required) {
        showToast('Please sign in first to generate shorts.', 'error');
        setTimeout(() => window.location.href = 'login.html?next=index.html', 700);
        return;
      }
    } catch {}
  }

  const btn = document.getElementById('autoGenerateBtn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="status-indicator-spinner" style="width:18px;height:18px;border-width:2px;"></span> Generating...`;
  }

  window.isGeneratingShorts = true;

  const monitorCard = document.getElementById('pipelineMonitorCard');
  if (monitorCard) {
    monitorCard.style.display = 'block';
    monitorCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // Reset UI elements
  const spinner = document.getElementById('monitorSpinner');
  if (spinner) spinner.style.display = '';
  const title = document.getElementById('monitorTitle');
  if (title) title.textContent = 'Creating Your Viral Shorts...';
  const subtitle = document.getElementById('monitorSubtitle');
  if (subtitle) subtitle.textContent = 'Analyzing transcript, ranking hooks, centering speakers, and generating captions.';
  const pb = document.getElementById('monitorProgressBar');
  if (pb) { pb.style.width = '5%'; pb.style.background = ''; }

  // Restore stepper labels (script-to-video may overwrite them)
  const _stepLabels = { 'step-download': '1. Download', 'step-transcribe': '2. Transcribe', 'step-select': '3. Select Clips', 'step-rank': '4. AI Ranking', 'step-render': '5. 9:16 Render' };
  for (const [id, label] of Object.entries(_stepLabels)) {
    const el = document.getElementById(id);
    if (el) { el.querySelector('.stepper-step-label').textContent = label; el.style.display = ''; }
  }

  resetSteppers('step-download');
  showToast('Started generating shorts!', 'info');
  showGlobalLoader('Initializing 9:16 Video Pipeline...', 'Connecting to YouTube downloader, AI diarization, and face-tracking models.');

  const deviceId = (typeof getDeviceId === 'function' ? getDeviceId() : localStorage.getItem('ps_device_id') || '');
  const payload = { url: url, num_shorts: 'all', clear_existing: true, device_id: deviceId };
  let targetBase = API_BASE || window.location.origin || 'http://localhost:5000';
  if (!targetBase || targetBase === 'null' || targetBase.startsWith('file:')) targetBase = 'http://localhost:5000';

  try {
    const headers = (typeof authHeaders === 'function' ? authHeaders() : {});
    headers['Content-Type'] = 'application/json';
    if (deviceId) headers['X-Device-Id'] = deviceId;

    const res = await fetch(`${targetBase}/api/pipeline/auto-generate`, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload)
    });

    const data = await res.json().catch(() => ({}));
    hideGlobalLoader();

    if (res.status === 401) {
      window.isGeneratingShorts = false;
      showToast(data.detail || 'Please login to generate', 'error');
      setTimeout(() => window.location.href = 'login.html', 800);
      resetGenerateBtn();
      return;
    }
    if (res.status === 403) {
      window.isGeneratingShorts = false;
      showToast(data.detail || 'Trial limit reached on this device', 'error');
      resetGenerateBtn();
      return;
    }
    if (!res.ok) throw new Error(data.detail || data.error || data.message || `HTTP ${res.status}`);

    if (data.success) {
      pollPipelineProgress();
    } else {
      window.isGeneratingShorts = false;
      showToast(`Pipeline: ${data.message || 'Error'}`, 'error');
      resetGenerateBtn();
    }
  } catch (err) {
    window.isGeneratingShorts = false;
    hideGlobalLoader();
    showToast(err.message || `Cannot reach backend at ${targetBase}`, 'error');
    resetGenerateBtn();
  }
};

function resetGenerateBtn(failed = false) {
  const btn = document.getElementById('autoGenerateBtn');
  if (btn) {
    btn.disabled = false;
    if (failed) {
      btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg> Retry Pipeline`;
      btn.style.borderColor = 'var(--red)';
      btn.style.color = 'var(--red)';
    } else {
      btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Generate Shorts`;
      btn.style.borderColor = '';
      btn.style.color = '';
    }
  }
}

/* ─── 5-Phase Stepper Controller ─────────────────────────────────────────── */
function updateStepperPhases(phase) {
  const phaseOrder = ['download', 'transcribe', 'select', 'rank', 'render'];
  const currentIndex = phaseOrder.indexOf(phase);

  phaseOrder.forEach((p, idx) => {
    const stepEl = document.getElementById(`step-${p}`);
    if (!stepEl) return;
    stepEl.classList.remove('active', 'completed');
    if (idx < currentIndex) {
      stepEl.classList.add('completed');
    } else if (idx === currentIndex) {
      stepEl.classList.add('active');
    }
  });
}

function resetSteppers(activeStepId = 'step-download') {
  const steps = document.querySelectorAll('.stepper-step');
  steps.forEach(s => s.classList.remove('active', 'completed', 'error'));
  const first = document.getElementById(activeStepId);
  if (first) first.classList.add('active');
}

/* ─── Pipeline Progress Monitoring ───────────────────────────────────────── */
function pollPipelineProgress() {
  if (pollInterval) clearInterval(pollInterval);
  if (sseSource) { try { sseSource.close(); } catch {} sseSource = null; }

  let sseFailed = false;
  try {
    const token = getAuthToken();
    const sseUrl = `${API_BASE}/api/pipeline/stream` + (token ? `?token=${encodeURIComponent(token)}` : '');
    if (window.EventSource) {
      const es = new EventSource(sseUrl);
      sseSource = es;
      es.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          const p = data.pipeline;
          if (p) updateMonitorUI(p);
          if (p && (p.status === 'completed' || p.status === 'error')) {
            es.close();
            sseSource = null;
            if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
          }
        } catch {}
      };
      es.onerror = () => {
        es.close();
        sseSource = null;
        if (!pollInterval) startPoll();
      };
      setTimeout(() => {
        if (sseSource && sseSource.readyState !== 1) {
          try { sseSource.close(); } catch {}
          sseSource = null;
          startPoll();
        }
      }, 2500);
    } else {
      sseFailed = true;
    }
  } catch {
    sseFailed = true;
  }

  if (sseFailed || !window.EventSource) startPoll();

  function startPoll() {
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(async () => {
      try {
        const headers = {};
        const tk = getAuthToken();
        if (tk) headers['Authorization'] = `Bearer ${tk}`;
        const res = await fetch(`${API_BASE}/api/status`, { headers, cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();
        const p = data.pipeline;
        if (!p) return;
        updateMonitorUI(p);
        if (p.status === 'completed' || p.status === 'error') {
          clearInterval(pollInterval);
          pollInterval = null;
          if (sseSource) { try { sseSource.close(); } catch {} sseSource = null; }
        }
      } catch (e) {}
    }, 1000);
  }

  function updateMonitorUI(p) {
    const progressBar = document.getElementById('monitorProgressBar');
    const phaseText = document.getElementById('monitorPhaseText');
    const percentText = document.getElementById('monitorPercentText');
    const logBox = document.getElementById('monitorLiveLog');
    const monitorCard = document.getElementById('pipelineMonitorCard');
    const title = document.getElementById('monitorTitle');
    const subtitle = document.getElementById('monitorSubtitle');
    const spinner = document.getElementById('monitorSpinner');

    if (progressBar) progressBar.style.width = (p.progress || 5) + '%';
    if (percentText) percentText.textContent = (p.progress || 5) + '%';

    const phaseDescriptions = {
      download: 'Phase 1/5: Downloading source video in high definition...',
      transcribe: 'Phase 2/5: Transcribing speech with Whisper / AssemblyAI...',
      select: 'Phase 3/5: Identifying high-potential segments...',
      rank: 'Phase 4/5: Scoring virality hooks using semantic AI...',
      render: 'Phase 5/5: Face centering, cropping to 9:16 & burning captions...',
    };

    if (p.current_phase) {
      if (phaseText) phaseText.textContent = phaseDescriptions[p.current_phase] || 'Processing video...';
      updateStepperPhases(p.current_phase);
    }

    if (logBox && p.logs && p.logs.length > 0) {
      logBox.textContent = p.logs.join('\n');
      logBox.scrollTop = logBox.scrollHeight;
    }

    if (p.status === 'running') {
      window.isGeneratingShorts = true;
    } else if (p.status === 'completed') {
      window.isGeneratingShorts = false;
      if (progressBar) progressBar.style.width = '100%';
      if (percentText) percentText.textContent = '100%';
      if (spinner) spinner.style.display = 'none';
      if (title) title.textContent = '🎉 All Shorts Generated!';
      if (subtitle) subtitle.textContent = 'Your 9:16 clips are ready in the gallery below. Preview, stream, or download.';
      if (phaseText) phaseText.textContent = '✓ Completed Successfully';

      const steps = document.querySelectorAll('.stepper-step');
      steps.forEach(s => s.classList.add('completed'));

      showToast('Shorts ready! Scroll down to view gallery.', 'success');
      resetGenerateBtn();
      refreshOutputs();

      setTimeout(() => {
        const gallery = document.getElementById('gallerySection');
        if (gallery) gallery.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 1500);
    } else if (p.status === 'error') {
      window.isGeneratingShorts = false;
      if (spinner) spinner.style.display = 'none';
      const failedPhase = p.current_phase || 'unknown';
      const phaseNames = { download: 'Video Download', transcribe: 'Transcription', select: 'Clip Selection', rank: 'Semantic Ranking', render: 'Rendering' };
      const failedName = phaseNames[failedPhase] || failedPhase;
      if (title) title.textContent = '❌ Failed at: ' + failedName;
      if (subtitle) subtitle.textContent = (p.error || 'An error occurred during pipeline execution.') + '\nFix the issue and retry.';
      if (progressBar) progressBar.style.background = 'var(--red)';

      // Mark failed step red, keep completed ones green
      const phaseOrder = ['download', 'transcribe', 'select', 'rank', 'render'];
      const failIdx = phaseOrder.indexOf(failedPhase);
      phaseOrder.forEach((ph, idx) => {
        const el = document.getElementById('step-' + ph);
        if (!el) return;
        el.classList.remove('active');
        if (idx < failIdx) el.classList.add('completed');
        else if (idx === failIdx) { el.classList.add('error'); }
      });

      showToast('Pipeline failed at: ' + failedName + ' — ' + (p.error || ''), 'error');
      resetGenerateBtn(true);
    }
  }
}

/* ─── Active Generation Navigation Guard ──────────────────────────────────── */
window.isGeneratingShorts = false;

window.addEventListener('beforeunload', (e) => {
  if (window.isGeneratingShorts) {
    e.preventDefault();
    e.returnValue = '';
    return '';
  }
});

window.addEventListener('pagehide', () => {
  if (window.isGeneratingShorts) {
    try {
      const token = typeof getAuthToken === 'function' ? getAuthToken() : '';
      const blob = new Blob([], { type: 'application/json' });
      navigator.sendBeacon(`${API_BASE}/api/pipeline/cancel` + (token ? `?token=${encodeURIComponent(token)}` : ''), blob);
    } catch {}
  }
});

document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href]');
  if (link && window.isGeneratingShorts) {
    const href = link.getAttribute('href');
    if (href && !href.startsWith('#') && !href.startsWith('javascript:')) {
      e.preventDefault();
      showConfirmModal({
        title: '⚠️ Generation In Progress',
        message: 'A viral short is actively being generated. If you leave now, the pipeline will be TERMINATED and all progress will be lost.\n\nAre you sure you want to leave?',
        icon: '⚠️',
        confirmText: 'Yes, Stop & Leave',
        cancelText: 'Stay & Continue',
        confirmType: 'danger',
        onConfirm: async () => {
          window.isGeneratingShorts = false;
          try {
            const token = typeof getAuthToken === 'function' ? getAuthToken() : '';
            await fetch(`${API_BASE}/api/pipeline/cancel`, {
              method: 'POST',
              headers: token ? { 'Authorization': `Bearer ${token}` } : {},
            });
          } catch {}
          window.location.href = href;
        }
      });
    }
  }
});

// Intercept browser refresh (F5 / Ctrl+R) with custom modal
document.addEventListener('keydown', (e) => {
  if (window.isGeneratingShorts && (e.key === 'F5' || (e.ctrlKey && e.key === 'r'))) {
    e.preventDefault();
    showConfirmModal({
      title: '⚠️ Generation In Progress',
      message: 'Refreshing the page will TERMINATE the running pipeline. All progress will be lost.\n\nRefresh anyway?',
      icon: '⚠️',
      confirmText: 'Yes, Refresh & Stop',
      cancelText: 'Stay & Continue',
      confirmType: 'danger',
      onConfirm: async () => {
        window.isGeneratingShorts = false;
        try {
          const token = typeof getAuthToken === 'function' ? getAuthToken() : '';
          await fetch(`${API_BASE}/api/pipeline/cancel`, {
            method: 'POST',
            headers: token ? { 'Authorization': `Bearer ${token}` } : {},
          });
        } catch {}
        window.location.reload();
      }
    });
  }
});

/* ─── In-App Password Change Modal Helper ─────────────────────────────────── */
window.openChangePasswordModal = function() {
  let modal = document.getElementById('changePasswordModal');
  if (modal) modal.style.display = 'flex';
};

window.closeChangePasswordModal = function(e) {
  if (e && e.target && e.target.id !== 'changePasswordModal' && !e.target.classList.contains('modal-close-btn')) return;
  const modal = document.getElementById('changePasswordModal');
  if (modal) modal.style.display = 'none';
};

window.submitChangePassword = async function() {
  const old_password = document.getElementById('cp_old_password')?.value;
  const new_password = document.getElementById('cp_new_password')?.value;
  const confirm_password = document.getElementById('cp_confirm_password')?.value;

  if (!old_password) return showToast('Please enter your current password.', 'warning');
  if (!new_password || new_password.length < 8) return showToast('New password must be at least 8 characters long.', 'warning');
  if (new_password !== confirm_password) return showToast('New passwords do not match. Please re-enter.', 'warning');

  showGlobalLoader('Updating Password...', 'Encrypting new credentials.');
  try {
    const r = await fetch(`${API_BASE}/api/auth/change-password`, {
      method: 'POST',
      headers: (typeof authHeaders === 'function' ? authHeaders() : { 'Content-Type': 'application/json' }),
      body: JSON.stringify({ old_password, new_password })
    });
    const j = await r.json();
    hideGlobalLoader();
    if (!r.ok) throw new Error(j.detail || 'Failed to change password');

    showToast('Password changed successfully!', 'success');
    window.closeChangePasswordModal();
    if (document.getElementById('cp_old_password')) document.getElementById('cp_old_password').value = '';
    if (document.getElementById('cp_new_password')) document.getElementById('cp_new_password').value = '';
    if (document.getElementById('cp_confirm_password')) document.getElementById('cp_confirm_password').value = '';
  } catch (err) {
    hideGlobalLoader();
    showToast(err.message || 'Failed to change password', 'error');
  }
};

/* ─── Gallery Refresh & Rendering ────────────────────────────────────────── */
window.refreshOutputs = async function() {
  try {
    const headers = {};
    const tk = getAuthToken();
    if (tk) headers['Authorization'] = `Bearer ${tk}`;

    let res = await fetch(`${API_BASE}/api/files/output`, { headers, cache: 'no-store' });
    if (!res.ok && res.status !== 401) {
      res = await fetch(`${API_BASE}/api/outputs`, { headers, cache: 'no-store' });
    }
    if (!res.ok) return;

    const data = await res.json();
    allRenderedClips = data.files || data.outputs || [];
    renderGalleryGrid(allRenderedClips);
  } catch (err) {
    console.error('Failed to load gallery outputs:', err);
  }
};

function renderGalleryGrid(clips) {
  const grid = document.getElementById('clipsGrid');
  if (!grid) return;

  if (!clips || clips.length === 0) {
    grid.innerHTML = `
      <div class="empty-gallery-card">
        <div class="empty-icon-wrap">🎬</div>
        <h3 style="font-size:1.35rem; font-weight:800;">No Shorts Generated Yet</h3>
        <p style="font-size:0.92rem; color:var(--text-muted); max-width:460px;">
          Paste a YouTube link above and click <strong>Generate Shorts</strong> to create your first vertical viral clips.
        </p>
      </div>
    `;
    return;
  }

  grid.innerHTML = '';
  clips.forEach((clip, idx) => {
    const filename = clip.name || clip.filename || '';
    if (!filename) return;
    const safeFilename = String(filename);
    const videoSrc = buildVideoSrc(safeFilename);
    const rankNum = idx + 1;
    const sizeStr = clip.size_mb ? `${Number(clip.size_mb).toFixed(1)} MB` : '';

    const card = document.createElement('div');
    card.className = 'clip-card';

    // Thumbnail Container with 9:16 Video Preview
    const thumbContainer = document.createElement('div');
    thumbContainer.className = 'clip-thumb-container';

    const vid = document.createElement('video');
    vid.className = 'clip-thumb-video';
    vid.src = `${videoSrc}#t=0.5`;
    vid.preload = 'metadata';
    vid.muted = true;
    vid.playsInline = true;

    // Hover auto-preview
    card.addEventListener('mouseenter', () => {
      vid.play().catch(() => {});
    });
    card.addEventListener('mouseleave', () => {
      vid.pause();
      vid.currentTime = 0.5;
    });

    const badge = document.createElement('span');
    badge.className = 'clip-duration-badge';
    badge.textContent = '9:16 HD';

    const overlay = document.createElement('div');
    overlay.className = 'clip-play-overlay';
    overlay.innerHTML = '<div class="play-circle-btn">▶</div>';

    thumbContainer.appendChild(vid);
    thumbContainer.appendChild(badge);
    thumbContainer.appendChild(overlay);

    // Meta Info Bar
    const infoBar = document.createElement('div');
    infoBar.className = 'clip-info-bar';

    const metaRow = document.createElement('div');
    metaRow.className = 'clip-meta-row';

    const rankTag = document.createElement('span');
    rankTag.className = 'clip-rank-tag';
    rankTag.innerHTML = `✨ Viral Short #${rankNum}`;

    const sizeTag = document.createElement('span');
    sizeTag.className = 'clip-size-tag';
    sizeTag.textContent = sizeStr;

    metaRow.appendChild(rankTag);
    metaRow.appendChild(sizeTag);

    // Actions Row
    const actionsRow = document.createElement('div');
    actionsRow.className = 'clip-actions-row';

    const previewBtn = document.createElement('button');
    previewBtn.className = 'clip-btn primary';
    previewBtn.innerHTML = '▶ Preview';
    previewBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      openVideoModal(safeFilename);
    });

    const downloadLink = document.createElement('a');
    downloadLink.className = 'clip-btn';
    downloadLink.href = videoSrc;
    downloadLink.download = safeFilename;
    downloadLink.innerHTML = '⬇️ Save';
    downloadLink.addEventListener('click', (e) => e.stopPropagation());

    const delBtn = document.createElement('button');
    delBtn.className = 'clip-btn clip-btn-icon btn-danger-outline';
    delBtn.title = 'Delete Short';
    delBtn.innerHTML = '🗑️';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSingleOutput(safeFilename);
    });

    actionsRow.appendChild(previewBtn);
    actionsRow.appendChild(downloadLink);
    actionsRow.appendChild(delBtn);

    infoBar.appendChild(metaRow);
    infoBar.appendChild(actionsRow);

    card.appendChild(thumbContainer);
    card.appendChild(infoBar);

    card.addEventListener('click', () => openVideoModal(safeFilename));
    grid.appendChild(card);
  });
}

/* ─── Clear All & Delete Actions with Glassmorphic Confirmation Modal ─────── */
window.clearAllOutputs = async function() {
  showConfirmModal({
    title: 'Clear All Generated Shorts',
    message: 'Are you sure you want to delete all rendered video clips in your gallery? This will free up storage space and cannot be recovered.',
    icon: '🗑️',
    confirmText: 'Yes, Delete All',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Deleting Gallery Clips...', 'Purging output files.');
      try {
        const headers = {};
        const tk = getAuthToken();
        if (tk) headers['Authorization'] = `Bearer ${tk}`;

        const res = await fetch(`${API_BASE}/api/files/output/clear`, { method: 'POST', headers });
        const data = await res.json().catch(() => ({}));
        hideGlobalLoader();
        if (res.status === 401) { showToast(data.detail || 'Login required', 'error'); return; }
        if (res.ok) {
          showToast('All shorts cleared successfully', 'success');
          refreshOutputs();
        } else {
          showToast(data.detail || 'Failed to clear', 'error');
        }
      } catch (err) {
        hideGlobalLoader();
        showToast('Failed to clear outputs: ' + err.message, 'error');
      }
    }
  });
};

window.deleteSingleOutput = async function(filename) {
  showConfirmModal({
    title: 'Delete Short Video',
    message: `Are you sure you want to permanently delete "${filename}"?`,
    icon: '🎬',
    confirmText: 'Delete Short',
    confirmType: 'danger',
    onConfirm: async () => {
      showGlobalLoader('Deleting Short...', 'Removing file from disk.');
      try {
        const headers = {};
        const tk = getAuthToken();
        if (tk) headers['Authorization'] = `Bearer ${tk}`;
        headers['Content-Type'] = 'application/json';

        const res = await fetch(`${API_BASE}/api/files/output/delete`, {
          method: 'POST',
          headers,
          body: JSON.stringify({ filename })
        });
        const data = await res.json().catch(() => ({}));
        hideGlobalLoader();
        if (res.status === 401) { showToast(data.detail || 'Login required', 'error'); return; }
        if (res.ok) {
          showToast(`Deleted ${escapeHtml(filename)}`, 'success');
          refreshOutputs();
        } else {
          showToast(data.detail || 'Failed to delete', 'error');
        }
      } catch (err) {
        hideGlobalLoader();
        showToast(`Failed to delete ${escapeHtml(filename)}`, 'error');
      }
    }
  });
};

/* ─── Topic / Prompt To Short Generator (No Video Needed Mode) ───────────── */
window.generateFromTopic = async function() {
  const topic = document.getElementById('topicInput')?.value.trim();
  const niche = document.getElementById('topicNiche')?.value || 'Mindset & Psychology';
  const tone = document.getElementById('topicTone')?.value || 'High Energy Viral';
  const duration = parseInt(document.getElementById('topicDuration')?.value || '45', 10);

  if (!topic) {
    showToast('Please enter a topic or concept for your short.', 'warning');
    document.getElementById('topicInput')?.focus();
    return;
  }

  showGlobalLoader('Crafting Viral AI Script...', 'Structuring 3-second hook, high-retention body, and SEO tags.');
  try {
    const res = await fetch(`${API_BASE}/api/pipeline/generate-from-topic`, {
      method: 'POST',
      headers: (typeof authHeaders === 'function' ? authHeaders() : { 'Content-Type': 'application/json' }),
      body: JSON.stringify({ topic, niche, tone, duration })
    });
    const data = await res.json();
    hideGlobalLoader();

    if (!res.ok) throw new Error(data.detail || 'Failed to generate script');

    // Display the generated script in an interactive modal / container
    showScriptModal(data.generated_script, topic);
    showToast('AI Script generated successfully!', 'success');
  } catch (err) {
    hideGlobalLoader();
    showToast('Script Generation: ' + err.message, 'error');
  }
};

function showScriptModal(scriptText, topicTitle) {
  let modal = document.getElementById('scriptResultModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'scriptResultModal';
    modal.className = 'video-modal-backdrop';
    modal.innerHTML = `
      <div class="video-modal-content" style="max-width:720px; width:95%; max-height:85vh; display:flex; flex-direction:column;" onclick="event.stopPropagation()">
        <div class="video-modal-header">
          <h3 id="scriptModalTitle" class="modal-title" style="font-size:1.15rem;">✨ AI Generated Viral Script</h3>
          <button class="modal-close-btn" onclick="document.getElementById('scriptResultModal').style.display='none'">✕</button>
        </div>
        <div style="padding:1.5rem; flex:1; overflow-y:auto;">
          <p style="font-size:0.85rem; color:var(--text-muted); margin-bottom:0.75rem;">Here is your structured, retention-optimized short script ready for voiceover or automated generation:</p>
          <pre id="scriptModalContent" style="white-space:pre-wrap; background:#07090f; padding:1.2rem; border-radius:10px; font-family:var(--font-body); font-size:0.92rem; line-height:1.6; color:#e2e8f0; border:1px solid var(--border-medium);"></pre>
        </div>
        <div class="video-modal-footer" style="justify-content:space-between; gap:0.75rem;">
          <button class="btn-primary btn-sm" id="scriptVideoBtn" onclick="generateVideoFromScript()" style="min-width:200px;">🎬 Generate Video Shorts</button>
          <button class="btn-primary btn-sm" onclick="copyScriptText()" style="min-width:180px;">📋 Copy Full Script</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
  }

  const titleEl = document.getElementById('scriptModalTitle');
  const contentEl = document.getElementById('scriptModalContent');
  if (titleEl) titleEl.textContent = `✨ Viral Script: ${topicTitle.slice(0, 40)}`;
  if (contentEl) contentEl.textContent = scriptText;

  // Store script text for the generate button
  modal.dataset.script = scriptText;
  modal.dataset.topic = topicTitle;

  const videoBtn = document.getElementById('scriptVideoBtn');
  if (videoBtn) {
    videoBtn.disabled = false;
    videoBtn.textContent = '🎬 Generate Video Shorts';
    videoBtn.style.opacity = '1';
  }

  modal.style.display = 'flex';
  modal.onclick = (e) => {
    if (e.target.id === 'scriptResultModal') modal.style.display = 'none';
  };
}

window.copyScriptText = function() {
  const content = document.getElementById('scriptModalContent')?.textContent || '';
  if (navigator.clipboard) {
    navigator.clipboard.writeText(content);
    showToast('Script copied to clipboard! 📋', 'success');
  }
};

window.generateVideoFromScript = async function() {
  const modal = document.getElementById('scriptResultModal');
  if (!modal) return;

  const scriptText = modal.dataset.script || '';
  const topicTitle = modal.dataset.topic || 'Script';
  const btn = document.getElementById('scriptVideoBtn');
  if (!btn) return;

  if (!scriptText.trim()) {
    showToast('Script is empty — cannot generate video.', 'error');
    return;
  }

  // Prevent accidental navigation during generation
  window.isGeneratingShorts = true;

  // Close script modal
  modal.style.display = 'none';

  // Show pipeline monitor card with script-to-video stepper labels
  const monitorCard = document.getElementById('pipelineMonitorCard');
  if (monitorCard) monitorCard.style.display = 'block';

  // Rewrite stepper labels for script-to-video
  const stepDownload = document.getElementById('step-download');
  const stepTranscribe = document.getElementById('step-transcribe');
  const stepSelect = document.getElementById('step-select');
  const stepRank = document.getElementById('step-rank');
  const stepRender = document.getElementById('step-render');
  if (stepDownload) stepDownload.querySelector('.stepper-step-label').textContent = '1. TTS Voiceover';
  if (stepTranscribe) stepTranscribe.querySelector('.stepper-step-label').textContent = '2. Scene Generation';
  if (stepSelect) stepSelect.querySelector('.stepper-step-label').textContent = '3. Encoding';
  if (stepRank) { stepRank.querySelector('.stepper-step-label').textContent = '4. Finalize'; stepRank.style.display = ''; }
  if (stepRender) { stepRender.style.display = 'none'; }

  // Set titles
  const title = document.getElementById('monitorTitle');
  const subtitle = document.getElementById('monitorSubtitle');
  const phaseText = document.getElementById('monitorPhaseText');
  const percentText = document.getElementById('monitorPercentText');
  const logBox = document.getElementById('monitorLiveLog');
  const spinner = document.getElementById('monitorSpinner');
  if (title) title.textContent = 'Generating Script Video Short...';
  if (subtitle) subtitle.textContent = 'Synthesizing voiceover, generating AI scenes, and encoding 9:16 video.';
  if (phaseText) phaseText.textContent = 'Phase 1/3: Starting TTS voiceover synthesis...';
  if (percentText) percentText.textContent = '5%';
  if (spinner) spinner.style.display = '';
  if (logBox) logBox.textContent = '> Initializing script-to-video pipeline...';

  // Reset steppers to first step
  resetSteppers('step-download');

  // Animate progress while waiting
  const progressSteps = [
    { pct: 10, phase: 'download', text: 'Phase 1/3: Synthesizing TTS voiceover with word timing...', log: '> TTS engine processing voiceover text...' },
    { pct: 25, phase: 'download', text: 'Phase 1/3: TTS voiceover generating...', log: '> Voiceover synthesis in progress...' },
    { pct: 40, phase: 'transcribe', text: 'Phase 2/3: Generating AI scene backgrounds...', log: '> Scene generation starting...' },
    { pct: 55, phase: 'transcribe', text: 'Phase 2/3: Creating scene visuals per section...', log: '> Rendering AI scene images for each section...' },
    { pct: 70, phase: 'select', text: 'Phase 3/3: Encoding video with word-synced captions...', log: '> FFmpeg encoding HD 9:16 video...' },
    { pct: 85, phase: 'select', text: 'Phase 3/3: Applying animations and captions...', log: '> Zoompan animations, caption overlay...' },
    { pct: 95, phase: 'select', text: 'Phase 3/3: Finalizing video output...', log: '> Writing final MP4 to disk...' },
  ];
  let progressIdx = 0;
  const progressTimer = setInterval(() => {
    if (progressIdx >= progressSteps.length) return;
    const s = progressSteps[progressIdx];
    const pb = document.getElementById('monitorProgressBar');
    if (pb) pb.style.width = s.pct + '%';
    if (percentText) percentText.textContent = s.pct + '%';
    if (phaseText) phaseText.textContent = s.text;
    updateStepperPhases(s.phase);
    if (logBox) { logBox.textContent += '\n' + s.log; logBox.scrollTop = logBox.scrollHeight; }
    progressIdx++;
  }, 4000);

  // Disable button
  btn.disabled = true;
  btn.textContent = '⏳ Generating...';
  btn.style.opacity = '0.6';

  try {
    const token = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers['Authorization'] = 'Bearer ' + token;

    const res = await fetch('/api/pipeline/script-to-video', {
      method: 'POST',
      headers,
      body: JSON.stringify({ script: scriptText }),
    });

    const data = await res.json();

    clearInterval(progressTimer);

    if (!res.ok || !data.success) {
      throw new Error(data.detail || data.message || 'Generation failed');
    }

    // Mark complete
    const pb = document.getElementById('monitorProgressBar');
    if (pb) { pb.style.width = '100%'; pb.style.background = 'linear-gradient(90deg, #10b981, #34d399)'; }
    if (percentText) percentText.textContent = '100%';
    if (phaseText) phaseText.textContent = '✅ Complete!';
    if (spinner) spinner.style.display = 'none';
    if (logBox) { logBox.textContent += `\n> ✅ Video created: ${data.filename} (${data.duration.toFixed(1)}s) - ${data.width}x${data.height}`; logBox.scrollTop = logBox.scrollHeight; }
    if (title) title.textContent = 'Video Short Created!';
    const sizeMB = data.size_bytes ? (data.size_bytes / 1048576).toFixed(1) : '?';
    if (subtitle) subtitle.textContent = `${data.filename} • ${data.duration.toFixed(1)}s • ${sizeMB} MB`;

    showToast(`Video created: ${data.filename} (${data.duration.toFixed(1)}s)`, 'success');

    // Refresh gallery immediately
    if (typeof refreshOutputs === 'function') refreshOutputs();

    // Auto-hide monitor after 4s and scroll to gallery
    setTimeout(() => {
      if (monitorCard) monitorCard.style.display = 'none';
      // Reset stepper labels back to YouTube defaults
      const _stepLabels = { 'step-download': '1. Download', 'step-transcribe': '2. Transcribe', 'step-select': '3. Select Clips', 'step-rank': '4. AI Ranking', 'step-render': '5. 9:16 Render' };
      for (const [id, label] of Object.entries(_stepLabels)) {
        const el = document.getElementById(id);
        if (el) { el.querySelector('.stepper-step-label').textContent = label; el.style.display = ''; }
      }
      const gallery = document.getElementById('gallerySection');
      if (gallery) gallery.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 4000);

  } catch (err) {
    clearInterval(progressTimer);

    const pb = document.getElementById('monitorProgressBar');
    if (pb) { pb.style.width = '100%'; pb.style.background = 'var(--red)'; }
    if (phaseText) phaseText.textContent = '❌ Failed: ' + err.message;
    if (spinner) spinner.style.display = 'none';
    if (logBox) { logBox.textContent += `\n> ❌ ERROR: ${err.message}`; logBox.scrollTop = logBox.scrollHeight; }
    if (title) title.textContent = 'Generation Failed';
    if (subtitle) subtitle.textContent = err.message;

    window.isGeneratingShorts = false;
    showToast('Video Generation: ' + err.message, 'error');
  }

  // Re-enable button
  btn.disabled = false;
  btn.textContent = '🎬 Generate Video Shorts';
  btn.style.opacity = '1';
};

/* ─── Fullscreen Video Modal ─────────────────────────────────────────────── */
window.openVideoModal = function(filename) {
  const modal = document.getElementById('videoModal');
  const player = document.getElementById('modalVideoPlayer');
  const title = document.getElementById('modalVideoTitle');
  const dlBtn = document.getElementById('modalDownloadBtn');
  if (!modal || !player) return;

  const safe = String(filename);
  const videoUrl = buildVideoSrc(safe);
  player.src = videoUrl;
  player.load();
  player.play().catch(() => {});

  if (title) title.textContent = safe;
  if (dlBtn) { dlBtn.href = videoUrl; dlBtn.download = safe; }

  modal.style.display = 'flex';
  document.body.style.overflow = 'hidden';
};

window.closeVideoModal = function(e) {
  if (e && e.target && e.target.id !== 'videoModal' && !e.target.classList.contains('modal-close-btn')) return;
  const modal = document.getElementById('videoModal');
  const player = document.getElementById('modalVideoPlayer');
  if (player) { player.pause(); player.src = ''; }
  if (modal) modal.style.display = 'none';
  document.body.style.overflow = 'auto';
};

/* ─── Global Toast Fallback (Uses auth.js primary) ────────────────────────── */
if (typeof window.showToast !== 'function') {
  window.showToast = function(msg, type = 'info') {
    alert(msg);
  };
}
