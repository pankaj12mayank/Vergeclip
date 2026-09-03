/* ==========================================================================
   auth.js — PodcastShorts AI Authentication & Global UX Engine
   Features:
   - Unified Single-Login for User & Admin with automatic role routing
   - Global 3D Cinematic Motion Loader Overlay
   - Bottom-Right Human-Friendly Toast Notifications
   - Universal Password & Key Eye Toggle Helpers
   ========================================================================== */

const AUTH_TOKEN_KEY = 'ps_auth_token';
const AUTH_USER_KEY = 'ps_auth_user';

function getToken() { return localStorage.getItem(AUTH_TOKEN_KEY) || ''; }
function setToken(t) { localStorage.setItem(AUTH_TOKEN_KEY, t); }
function clearAuth() {
  localStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_USER_KEY);
}

function _decodeJwtPayload(token) {
  if (!token) return null;
  try {
    const part = token.split('.')[1];
    if (!part) return null;
    let b64 = part.replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4 !== 0) b64 += '=';
    return JSON.parse(atob(b64));
  } catch { return null; }
}

function isTokenExpired(token) {
  const payload = _decodeJwtPayload(token);
  if (!payload || !payload.exp) return true;
  return Date.now() >= payload.exp * 1000;
}

function getTokenTimeLeft(token) {
  const payload = _decodeJwtPayload(token);
  if (!payload || !payload.exp) return 0;
  return Math.max(0, payload.exp * 1000 - Date.now());
}

function autoLogoutIfExpired() {
  const t = getToken();
  if (t && isTokenExpired(t)) {
    clearAuth();
    if (typeof showToast === 'function') showToast('Session expired. Please sign in again.', 'warning');
    setTimeout(() => { if (!/login\.html|signup\.html/.test(location.pathname)) location.href = 'login.html'; }, 1200);
    return true;
  }
  return false;
}

async function autoRefreshToken() {
  const t = getToken();
  if (!t) return;
  if (isTokenExpired(t)) {
    autoLogoutIfExpired();
    return;
  }
  const timeLeft = getTokenTimeLeft(t);
  if (timeLeft < 30 * 60 * 1000) {
    try {
      const r = await fetch(`${getApiBase()}/api/auth/refresh`, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json' }
      });
      const j = await r.json();
      if (j.access_token) {
        setToken(j.access_token);
        const u = getUser();
        if (u) setUser({ ...u, token_refreshed_at: new Date().toISOString() });
      } else if (r.status === 401) {
        autoLogoutIfExpired();
      }
    } catch {}
  }
}

setInterval(() => { autoLogoutIfExpired() || autoRefreshToken(); }, 5 * 60 * 1000);

// ─── Automatic-auth fetch: attaches token + device-id, and on a stale token
// (401 / guest-trial 403) silently refreshes once and retries. Guarantees the
// admin/system owner never sees "403 Forbidden" just because the token expired.
async function authFetch(url, options = {}, _retried = false) {
  const opts = { ...options, headers: authHeaders(options.headers || {}) };
  const res = await fetch(url, opts);
  if (!_retried && (res.status === 401 || res.status === 403) && getToken()) {
    try {
      const rr = await fetch(`${getApiBase()}/api/auth/refresh`, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + getToken(), 'Content-Type': 'application/json' }
      });
      const jj = await rr.json();
      if (jj.access_token) {
        setToken(jj.access_token);
        return authFetch(url, options, true);
      }
    } catch {}
    // Refresh failed (hard-expired token) — clear and ask to sign in again.
    clearAuth();
    if (!/login\.html|signup\.html/.test(location.pathname)) location.href = 'login.html';
  }
  return res;
}

function getUser() {
  try { return JSON.parse(localStorage.getItem(AUTH_USER_KEY) || 'null'); } catch { return null; }
}
function setUser(u) { localStorage.setItem(AUTH_USER_KEY, JSON.stringify(u)); }
function isLoggedIn() { return !!getToken(); }

function getDeviceId() {
  let id = localStorage.getItem('ps_device_id');
  if (id) return id;
  try {
    const raw = [
      navigator.userAgent || '',
      screen.width + 'x' + screen.height,
      navigator.language || '',
      Intl.DateTimeFormat().resolvedOptions().timeZone || '',
      String(navigator.hardwareConcurrency || ''),
      navigator.platform || ''
    ].join('|');
    let hash = 0;
    for (let i = 0; i < raw.length; i++) {
      hash = ((hash << 5) - hash) + raw.charCodeAt(i);
      hash |= 0;
    }
    id = 'dev_' + Math.abs(hash).toString(36) + '_' + Date.now().toString(36).slice(-4);
  } catch {
    id = 'dev_' + Math.random().toString(36).slice(2, 10);
  }
  localStorage.setItem('ps_device_id', id);
  return id;
}

function getApiBase() {
  const saved = localStorage.getItem('CUSTOM_API_BASE');
  if (saved) return saved;
  // If we are on the FastAPI server port 5000 or a live production domain with no dev port
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

function authHeaders(extra = {}) {
  const t = getToken();
  const h = { ...extra };
  if (t) h['Authorization'] = `Bearer ${t}`;
  const dev = getDeviceId();
  if (dev) h['X-Device-Id'] = dev;
  if (!h['Content-Type'] && !(extra['Content-Type'])) h['Content-Type'] = 'application/json';
  return h;
}

function escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ─── 1. Human-Friendly Error Message Translator ─────────────────────────── */
function formatHumanMessage(rawMsg, defaultType = 'info') {
  if (!rawMsg) return 'Action completed.';
  const str = String(rawMsg).trim();

  // Known pattern mappings
  if (/invalid.*password|invalid.*identifier|incorrect.*password/i.test(str)) {
    return 'Invalid username/email or password. Please verify your credentials.';
  }
  if (/user.*not.*found/i.test(str)) {
    return 'No registered account found with those details.';
  }
  if (/username.*already.*taken|username.*exists/i.test(str)) {
    return 'This username is already taken. Please choose another username.';
  }
  if (/email.*already.*registered|email.*exists/i.test(str)) {
    return 'An account with this email address already exists. Try signing in.';
  }
  if (/password.*least.*8/i.test(str)) {
    return 'Please choose a stronger password with at least 8 characters.';
  }
  if (/passwords.*not.*match/i.test(str)) {
    return 'Passwords do not match. Please re-enter your password carefully.';
  }
  if (/trial.*used|trial.*limit/i.test(str)) {
    return 'Your device trial limit has been reached. Please sign in or upgrade.';
  }
  if (/token.*expired|session.*expired/i.test(str)) {
    return 'Your session has expired. Please sign in again to continue.';
  }
  if (/failed to fetch|networkerror|cannot reach/i.test(str)) {
    return 'Unable to reach backend server. Please verify your internet connection or server URL.';
  }
  if (/admin access required|forbidden/i.test(str)) {
    return 'Administrator privileges are required to access this area.';
  }
  if (/all.*shorts.*cleared/i.test(str)) {
    return 'All generated shorts have been removed successfully.';
  }
  if (/account.*created/i.test(str)) {
    return 'Account created successfully! Welcome aboard.';
  }
  if (/welcome.*back/i.test(str)) {
    return str;
  }
  return str.replace(/^HTTP \d+:\s*/, '').replace(/^{.*"detail":\s*"([^"]+)".*}$/, '$1');
}

/* ─── 8. Strict Backend Server Connection Guard ────────────────────────────── */
window.isBackendOnline = false;

window.showBackendOfflineOverlay = function() {
  window.isBackendOnline = false;
  let overlay = document.getElementById('backendOfflineOverlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'backendOfflineOverlay';
    overlay.className = 'video-modal-backdrop';
    overlay.style.cssText = 'display:flex; z-index:999999; background:rgba(4,6,12,0.92); backdrop-filter:blur(24px); -webkit-backdrop-filter:blur(24px);';
    overlay.innerHTML = `
      <div class="video-modal-content" style="max-width:440px; text-align:center; padding:2.2rem; border-color:rgba(168,85,247,0.35); box-shadow:0 0 45px rgba(168,85,247,0.25);">
        <div style="font-size:2.8rem; margin-bottom:0.75rem; filter:drop-shadow(0 0 16px var(--purple-glow));">⚡</div>
        <h2 style="font-size:1.35rem; font-weight:800; color:#fff; margin-bottom:0.4rem;">Backend Server Disconnected</h2>
        <p style="font-size:0.9rem; color:var(--text-muted); line-height:1.5; margin-bottom:1.5rem;">
          Vergeclip API service is currently offline. Please ensure the backend server is running to continue.
        </p>
        <button class="btn-primary" onclick="window.pingBackendServer(true)" style="width:100%; justify-content:center;">
          ↻ Reconnect Now
        </button>
      </div>
    `;
    document.body.appendChild(overlay);
  } else {
    overlay.style.display = 'flex';
  }
};

window.hideBackendOfflineOverlay = function() {
  window.isBackendOnline = true;
  const overlay = document.getElementById('backendOfflineOverlay');
  if (overlay) overlay.style.display = 'none';
  updateAuthUI();
};

window.pingBackendServer = async function(manualClick = false) {
  const base = getApiBase();
  try {
    const res = await fetch(`${base}/api/health`, { cache: 'no-store' });
    if (res.ok) {
      hideBackendOfflineOverlay();
      if (manualClick) showToast('Backend server connected successfully! 🚀', 'success');
      return true;
    }
  } catch (e) {}

  window.isBackendOnline = false;
  updateAuthUI();
  showBackendOfflineOverlay();
  return false;
};

// Auto-run connection guard on DOM ready
document.addEventListener('DOMContentLoaded', () => {
  window.pingBackendServer();
  setInterval(() => window.pingBackendServer(), 4000);
});


/* ─── 2. Global Toast Notification System (Bottom-Right) ─────────────────── */
window.showToast = function(msg, type = 'info', duration = 4000) {
  let container = document.getElementById('toastContainer');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toastContainer';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }

  const humanText = formatHumanMessage(msg, type);
  const toast = document.createElement('div');
  toast.className = `toast-item ${type}`;

  const iconMap = {
    success: '✅',
    error: '❌',
    warning: '⚠️',
    info: '✨'
  };
  const titleMap = {
    success: 'Success',
    error: 'Notice',
    warning: 'Attention',
    info: 'Information'
  };

  const icon = iconMap[type] || 'ℹ️';
  const title = titleMap[type] || 'Notification';

  toast.innerHTML = `
    <div class="toast-icon">${icon}</div>
    <div class="toast-body">
      <div class="toast-title">${title}</div>
      <div class="toast-msg">${escapeHtml(humanText)}</div>
    </div>
    <button class="toast-close-btn" title="Dismiss">✕</button>
    <div class="toast-progress">
      <div class="toast-progress-bar" style="animation-duration: ${duration}ms;"></div>
    </div>
  `;

  const closeBtn = toast.querySelector('.toast-close-btn');
  closeBtn.addEventListener('click', () => removeToast(toast));

  container.appendChild(toast);

  const timer = setTimeout(() => {
    removeToast(toast);
  }, duration);

  function removeToast(el) {
    clearTimeout(timer);
    el.style.opacity = '0';
    el.style.transform = 'translateY(10px) scale(0.95)';
    setTimeout(() => {
      if (el.parentNode) el.parentNode.removeChild(el);
    }, 250);
  }
};

/* ─── 3. Global 3D Cinematic Motion Loader Overlay ────────────────────────── */
function ensureLoaderDOM() {
  let loader = document.getElementById('globalLoaderBackdrop');
  if (!loader) {
    loader = document.createElement('div');
    loader.id = 'globalLoaderBackdrop';
    loader.className = 'global-loader-backdrop';
    loader.innerHTML = `
      <div class="global-loader-card">
        <div class="loader-3d-stage">
          <div class="loader-cube-wrap">
            <div class="loader-cube">
              <div class="cube-face cube-front"></div>
              <div class="cube-face cube-back"></div>
              <div class="cube-face cube-right"></div>
              <div class="cube-face cube-left"></div>
              <div class="cube-face cube-top"></div>
              <div class="cube-face cube-bottom"></div>
            </div>
          </div>
          <div class="orb-core"></div>
          <div class="orbital-ring orbital-ring-1"></div>
          <div class="orbital-ring orbital-ring-2"></div>
          <div class="orbital-ring orbital-ring-3"></div>
        </div>
        <div class="loader-spectrum-wrap">
          <div class="spectrum-bar"></div>
          <div class="spectrum-bar"></div>
          <div class="spectrum-bar"></div>
          <div class="spectrum-bar"></div>
          <div class="spectrum-bar"></div>
          <div class="spectrum-bar"></div>
          <div class="spectrum-bar"></div>
        </div>
        <div class="loader-cinematic-text-wrap">
          <h3 id="globalLoaderTitle" class="loader-main-title">Processing AI Action...</h3>
          <p id="globalLoaderSub" class="loader-subtitle">Executing real-time neural pipeline and optimizing media assets.</p>
        </div>
        <div class="loader-progress-track">
          <div class="loader-progress-fill"></div>
        </div>
      </div>
    `;
    document.body.appendChild(loader);
  }
  return loader;
}

window.showGlobalLoader = function(title = 'Loading Vergeclip AI...', subtitle = 'Please hold on while we process your request.') {
  const loader = ensureLoaderDOM();
  const titleEl = document.getElementById('globalLoaderTitle');
  const subEl = document.getElementById('globalLoaderSub');
  if (titleEl) titleEl.textContent = title;
  if (subEl) subEl.textContent = subtitle;
  loader.classList.add('active');
};

window.hideGlobalLoader = function() {
  const loader = document.getElementById('globalLoaderBackdrop');
  if (loader) {
    loader.classList.remove('active');
  }
};

/* ─── 4. Universal Eye Toggle for Passwords & Keys ────────────────────────── */
window.togglePasswordVisibility = function(inputId, btnEl) {
  const input = document.getElementById(inputId);
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    if (btnEl) btnEl.textContent = '🔒';
  } else {
    input.type = 'password';
    if (btnEl) btnEl.textContent = '👁️';
  }
};

/* ─── 5. Unified Single-Login Flow ───────────────────────────────────────── */
async function handleLogin(e) {
  if (e) e.preventDefault();
  const id = document.getElementById('login_identifier')?.value.trim();
  const p = document.getElementById('login_password')?.value;
  const btn = document.getElementById('loginBtn');

  if (!id || !p) {
    showToast('Please enter both username/email and password.', 'warning');
    return;
  }

  showGlobalLoader('Authenticating...', 'Verifying your credentials and role permissions.');
  if (btn) { btn.disabled = true; btn.textContent = 'Signing in...'; }

  const candidates = Array.from(new Set([getApiBase(), 'http://localhost:5000', 'http://127.0.0.1:5000']));
  let lastError = null;

  for (const base of candidates) {
    try {
      const res = await fetch(`${base}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier: id, password: p })
      });

      if (res.status === 404) {
        continue;
      }

      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || 'Invalid username or password');

      localStorage.setItem('CUSTOM_API_BASE', base);
      setToken(data.access_token);
      setUser(data.user);

      hideGlobalLoader();
      const user = data.user;
      const urlParams = new URLSearchParams(window.location.search);
      const nextParam = urlParams.get('next');

      if (user.role === 'admin') {
        showToast(`Welcome Administrator ${user.username}! Directing to Admin Portal...`, 'success');
        setTimeout(() => {
          window.location.href = nextParam || 'admin.html';
        }, 600);
      } else {
        showToast(`Welcome back, ${user.username}!`, 'success');
        setTimeout(() => {
          const next = (nextParam && nextParam !== 'admin.html') ? nextParam : 'index.html';
          window.location.href = next;
        }, 600);
      }
      return;
    } catch (ex) {
      lastError = ex;
      if (ex.message && !ex.message.includes('Failed to fetch') && !ex.message.includes('404')) {
        break;
      }
    }
  }

  hideGlobalLoader();
  if (btn) { btn.disabled = false; btn.textContent = 'Sign In to Account'; }
  showToast(lastError ? lastError.message : 'Unable to connect to backend server. Ensure python server.py is running.', 'error');
}

async function handleSignup(e) {
  if (e) e.preventDefault();
  const u = document.getElementById('su_username')?.value.trim();
  const em = document.getElementById('su_email')?.value.trim();
  const p = document.getElementById('su_password')?.value;
  const pc = document.getElementById('su_password_confirm')?.value;
  const btn = document.getElementById('signupBtn');

  if (p !== pc) {
    showToast('Passwords do not match. Please confirm your password.', 'warning');
    return;
  }
  if (!p || p.length < 8) {
    showToast('Password must be at least 8 characters long.', 'warning');
    return;
  }

  showGlobalLoader('Creating Your Account...', 'Initializing user workspace and security credentials.');
  if (btn) { btn.disabled = true; btn.textContent = 'Creating Account...'; }

  const candidates = Array.from(new Set([getApiBase(), 'http://localhost:5000', 'http://127.0.0.1:5000']));
  let lastError = null;

  for (const base of candidates) {
    try {
      const res = await fetch(`${base}/api/auth/signup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: u, email: em, password: p })
      });

      if (res.status === 404) continue;

      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || data.error || 'Signup failed');

      localStorage.setItem('CUSTOM_API_BASE', base);
      setToken(data.access_token);
      setUser(data.user);

      hideGlobalLoader();
      showToast('Account created successfully! Welcome to Vergeclip.', 'success');
      setTimeout(() => window.location.href = 'index.html', 700);
      return;
    } catch (ex) {
      lastError = ex;
      if (ex.message && !ex.message.includes('Failed to fetch') && !ex.message.includes('404')) {
        break;
      }
    }
  }

  hideGlobalLoader();
  if (btn) { btn.disabled = false; btn.textContent = 'Create Free Account'; }
  showToast(lastError ? lastError.message : 'Unable to connect to backend server. Ensure python server.py is running.', 'error');
}

function handleLogout() {
  clearAuth();
  showToast('You have been signed out safely.', 'info');
  setTimeout(() => window.location.href = 'login.html', 500);
}

/* ─── 6. Global Glassmorphic Confirmation Modal System ─────────────────────── */
function ensureConfirmModalDOM() {
  let modal = document.getElementById('globalConfirmModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'globalConfirmModal';
    modal.className = 'video-modal-backdrop';
    modal.style.display = 'none';
    modal.innerHTML = `
      <div class="video-modal-content global-confirm-card" style="max-width:440px; padding:2rem; text-align:center; border-radius:var(--radius-lg);" onclick="event.stopPropagation()">
        <div id="confirmModalIcon" style="font-size:2.6rem; margin-bottom:1rem; filter:drop-shadow(0 0 16px var(--purple-glow));">⚠️</div>
        <h3 id="confirmModalTitle" style="font-family:var(--font-heading); font-size:1.35rem; font-weight:800; color:#fff; margin-bottom:0.6rem;">Confirm Action</h3>
        <p id="confirmModalMsg" style="font-size:0.92rem; color:var(--text-muted); line-height:1.5; margin-bottom:1.75rem;">Are you sure you want to proceed?</p>
        <div style="display:flex; justify-content:center; gap:0.85rem;">
          <button id="confirmCancelBtn" class="btn-outline btn-sm" style="min-width:110px;">Cancel</button>
          <button id="confirmAcceptBtn" class="btn-primary btn-sm" style="min-width:120px;">Confirm</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
  }
  return modal;
}

window.showConfirmModal = function({
  title = 'Confirm Action',
  message = 'Are you sure you want to proceed?',
  icon = '⚠️',
  confirmText = 'Confirm',
  cancelText = 'Cancel',
  confirmType = 'primary',
  onConfirm = null,
  onCancel = null
} = {}) {
  const modal = ensureConfirmModalDOM();
  const iconEl = document.getElementById('confirmModalIcon');
  const titleEl = document.getElementById('confirmModalTitle');
  const msgEl = document.getElementById('confirmModalMsg');
  const acceptBtn = document.getElementById('confirmAcceptBtn');
  const cancelBtn = document.getElementById('confirmCancelBtn');

  if (iconEl) iconEl.textContent = icon;
  if (titleEl) titleEl.textContent = title;
  if (msgEl) msgEl.textContent = message;
  if (acceptBtn) {
    acceptBtn.textContent = confirmText;
    acceptBtn.className = confirmType === 'danger' ? 'btn-outline btn-danger-outline' : 'btn-primary';
    acceptBtn.onclick = () => {
      modal.style.display = 'none';
      if (typeof onConfirm === 'function') onConfirm();
    };
  }
  if (cancelBtn) {
    cancelBtn.textContent = cancelText;
    cancelBtn.onclick = () => {
      modal.style.display = 'none';
      if (typeof onCancel === 'function') onCancel();
    };
  }

  modal.onclick = (e) => {
    if (e.target.id === 'globalConfirmModal') {
      modal.style.display = 'none';
      if (typeof onCancel === 'function') onCancel();
    }
  };

  modal.style.display = 'flex';
};

/* ─── 7. Sync UI Auth State on Page Load ─────────────────────────────────── */
async function updateAuthUI() {
  const authBox = document.getElementById('authBox');
  const authLoggedOut = document.getElementById('authLoggedOut');
  const authLoggedIn = document.getElementById('authLoggedIn');
  const userNameEl = document.getElementById('authUserName');
  const userAvatarEl = document.getElementById('userAvatar');

  // If backend is offline, always force logged out display in navbar
  if (!window.isBackendOnline) {
    if (authLoggedIn) authLoggedIn.style.display = 'none';
    if (authLoggedOut) authLoggedOut.style.display = 'flex';
    return;
  }

  const user = getUser();
  const logged = isLoggedIn();

  if (logged && user) {
    if (authLoggedOut) authLoggedOut.style.display = 'none';
    if (authLoggedIn) authLoggedIn.style.display = 'flex';

    const oldLink = document.getElementById('adminLink');
    if (oldLink) oldLink.remove();

    if (user.role === 'admin') {
      if (userNameEl) {
        userNameEl.innerHTML = `<span style="color:#fff; font-weight:800;">${escapeHtml(user.username)}</span> <span class="badge badge-green" style="font-size:0.65rem; padding:0.12rem 0.4rem; margin-left:0.3rem;">OWNER</span>`;
      }
      if (userAvatarEl) {
        userAvatarEl.textContent = '⚙️';
        userAvatarEl.style.background = 'var(--grad-primary)';
      }
      const pill = document.querySelector('.user-badge-pill');
      if (pill) {
        pill.style.cursor = 'pointer';
        pill.title = 'Click to open Admin Portal';
        pill.onclick = () => window.location.href = 'admin.html';
      }
    } else {
      if (userNameEl) userNameEl.textContent = user.username;
      if (userAvatarEl && user.username) {
        userAvatarEl.textContent = user.username.charAt(0).toUpperCase();
        userAvatarEl.style.background = 'rgba(255,255,255,0.08)';
      }
      const pill = document.querySelector('.user-badge-pill');
      if (pill) {
        pill.style.cursor = 'default';
        pill.title = '';
        pill.onclick = null;
      }
    }
  } else {
    if (authLoggedIn) authLoggedIn.style.display = 'none';
    if (authLoggedOut) authLoggedOut.style.display = 'flex';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  ensureLoaderDOM();
  ensureConfirmModalDOM();
  autoLogoutIfExpired();
  updateAuthUI();
});

// Window Exports
window.getToken = getToken;
window.setToken = setToken;
window.clearAuth = clearAuth;
window.getUser = getUser;
window.setUser = setUser;
window.isLoggedIn = isLoggedIn;
window.getDeviceId = getDeviceId;
window.getApiBase = getApiBase;
window.authHeaders = authHeaders;
window.handleSignup = handleSignup;
window.handleLogin = handleLogin;
window.handleLogout = handleLogout;
window.updateAuthUI = updateAuthUI;
window.formatHumanMessage = formatHumanMessage;
window.showConfirmModal = showConfirmModal;
window.isTokenExpired = isTokenExpired;
window.getTokenTimeLeft = getTokenTimeLeft;
window.autoLogoutIfExpired = autoLogoutIfExpired;
