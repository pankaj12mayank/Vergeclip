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

  resetSteppers('step-download');
  showToast('Started generating shorts!', 'info');

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
    if (res.status === 401) {
      showToast(data.detail || 'Please login to generate', 'error');
      setTimeout(() => window.location.href = 'login.html', 800);
      resetGenerateBtn();
      return;
    }
    if (res.status === 403) {
      showToast(data.detail || 'Trial limit reached on this device', 'error');
      resetGenerateBtn();
      return;
    }
    if (!res.ok) throw new Error(data.detail || data.error || data.message || `HTTP ${res.status}`);

    if (data.success) {
      pollPipelineProgress();
    } else {
      showToast(`Pipeline: ${data.message || 'Error'}`, 'error');
      resetGenerateBtn();
    }
  } catch (err) {
    showToast(err.message || `Cannot reach backend at ${targetBase}`, 'error');
    resetGenerateBtn();
  }
};

function resetGenerateBtn() {
  const btn = document.getElementById('autoGenerateBtn');
  if (btn) {
    btn.disabled = false;
    btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg> Generate Shorts`;
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
  steps.forEach(s => s.classList.remove('active', 'completed'));
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

    if (p.status === 'completed') {
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
      if (spinner) spinner.style.display = 'none';
      if (title) title.textContent = '❌ Generation Failed';
      if (subtitle) subtitle.textContent = p.error || 'An error occurred during pipeline execution. Check logs below.';
      if (progressBar) progressBar.style.background = 'var(--red)';
      showToast('Pipeline error: ' + (p.error || 'Unknown error'), 'error');
      resetGenerateBtn();
    }
  }
}

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
          <pre id="scriptModalContent" style="white-space:pre-wrap; background:#07090f; padding:1.2rem; border-radius:10px; font-family:'Inter',sans-serif; font-size:0.92rem; line-height:1.6; color:#e2e8f0; border:1px solid var(--border-medium);"></pre>
        </div>
        <div class="video-modal-footer" style="justify-content:flex-end;">
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
