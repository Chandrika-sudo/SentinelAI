/**
 * SentinelAI – frontend controller
 * Pure vanilla JS, no jQuery dependency.
 */

'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
const NUM_CAMERAS = 4;
let prevState = {}; // track previous camera states to detect transitions

// ── Init ───────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  updateClock();
  setInterval(updateClock, 1000);
  setInterval(pollStatus, 2000);

  // Set init timestamp in first log entry
  const initTimeEl = document.getElementById('init-time');
  if (initTimeEl) initTimeEl.textContent = now();

  // Load blank placeholders immediately
  for (let i = 1; i <= NUM_CAMERAS; i++) {
    loadFeed(i);
  }

  // Close menus on outside click
  document.addEventListener('click', e => {
    if (!e.target.closest('.cam-actions')) closeAllMenus();
  });
});

// ── Clock ──────────────────────────────────────────────────────────────────────
function updateClock() {
  const el = document.getElementById('sys-clock');
  if (el) el.textContent = new Date().toLocaleTimeString('en-GB');
}

function now() {
  return new Date().toLocaleTimeString('en-GB');
}

// ── Feed management ────────────────────────────────────────────────────────────
function loadFeed(cameraId) {
  const img = document.getElementById(`feed-${cameraId}`);
  if (img) img.src = `/feed/${cameraId}`;
}

// ── Status polling ─────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const res  = await fetch('/status');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    updateDashboard(data);
  } catch (err) {
    console.error('Status poll failed:', err);
  }
}

function updateDashboard(data) {
  let totalPeople = 0;
  let activeCams  = 0;
  let totalFalls  = 0;
  let totalTheft  = 0;
  let totalFire   = 0;

  for (let i = 1; i <= NUM_CAMERAS; i++) {
    const cam  = data[i] || { type: 'offline', count: 0, fall: false, theft: false, fire: false };
    const prev = prevState[i] || {};

    // People count
    const countEl = document.getElementById(`count-${i}`);
    if (countEl) countEl.textContent = `${cam.count} person${cam.count !== 1 ? 's' : ''}`;

    // Status indicator
    const dot      = document.getElementById(`dot-${i}`);
    const statusEl = document.getElementById(`status-${i}`);
    const isOnline = cam.type !== 'offline';

    if (isOnline) {
      activeCams++;
      dot?.classList.replace('offline', 'online');
      if (statusEl) { statusEl.textContent = 'LIVE'; statusEl.className = 'cam-status online'; }
    } else {
      dot?.classList.replace('online', 'offline');
      if (statusEl) { statusEl.textContent = 'OFFLINE'; statusEl.className = 'cam-status'; }
    }

    // Offline message overlay + live shimmer control
    const offlineMsg = document.getElementById(`offline-${i}`);
    const viewport   = document.getElementById(`feed-${i}`)?.parentElement;
    if (offlineMsg) offlineMsg.classList.toggle('hidden', isOnline);
    const feedImg = document.getElementById(`feed-${i}`);
    if (feedImg) feedImg.style.opacity = isOnline ? '1' : '0';
    if (viewport) viewport.classList.toggle('live', isOnline);

    // Card-level alert border glow
    const card = document.getElementById(`cam-${i}`);
    if (card) {
      card.classList.toggle('has-fire',  !!cam.fire);
      card.classList.toggle('has-alert', !cam.fire && (cam.fall || cam.theft));
    }

    // Alert badges
    document.getElementById(`alert-${i}`)?.classList.toggle('hidden', !cam.fall);
    document.getElementById(`theft-${i}`)?.classList.toggle('hidden', !cam.theft);
    document.getElementById(`fire-${i}`)?.classList.toggle('hidden',  !cam.fire);

    // Dismiss button – visible if any alert is active
    const anyAlert = cam.fall || cam.theft || cam.fire;
    document.getElementById(`reset-${i}`)?.classList.toggle('hidden', !anyAlert);

    // Dot + status chip colour
    if (anyAlert) {
      dot?.classList.add('alert');
      if (statusEl) statusEl.className = 'cam-status alert';
    } else if (isOnline) {
      dot?.classList.remove('alert');
    }

    // Log on leading edge only (false → true transition)
    if (cam.fall  && !prev.fall)  addLog(`Fall detected on CAM_0${i} — check immediately.`, 'danger');
    if (cam.theft && !prev.theft) addLog(`Theft alert on CAM_0${i} — object removed from scene.`, 'warn');
    if (cam.fire  && !prev.fire)  addLog(`FIRE on CAM_0${i} — evacuate immediately!`, 'danger');

    totalPeople += cam.count;
    if (cam.fall)  totalFalls++;
    if (cam.theft) totalTheft++;
    if (cam.fire)  totalFire++;
    prevState[i] = { ...cam };
  }

  // Topbar metrics + pill active state
  const setPill = (id, val) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    el.closest('.metric-pill')?.toggleAttribute('data-active', val > 0);
  };
  setPill('stat-people', totalPeople);
  setPill('stat-falls',  totalFalls);
  setPill('stat-theft',  totalTheft);
  setPill('stat-fire',   totalFire);
  document.getElementById('active-label').textContent =
    `— ${activeCams} of ${NUM_CAMERAS} cameras online`;

  // Nav badge
  const badgeCount = totalFalls + totalTheft + totalFire;
  const badge = document.getElementById('nav-badge');
  if (badge) {
    badge.textContent = badgeCount;
    badge.style.display = badgeCount > 0 ? 'inline' : 'none';
  }
}

// ── Camera controls ────────────────────────────────────────────────────────────
function toggleMenu(cameraId) {
  const menu = document.getElementById(`menu-${cameraId}`);
  const isOpen = menu.classList.contains('open');
  closeAllMenus();
  if (!isOpen) menu.classList.add('open');
}

function closeAllMenus() {
  for (let i = 1; i <= NUM_CAMERAS; i++) {
    document.getElementById(`menu-${i}`)?.classList.remove('open');
  }
}

async function uploadVideo(event, cameraId) {
  const file = event.target.files[0];
  if (!file) return;
  closeAllMenus();

  const formData = new FormData();
  formData.append('file', file);
  formData.append('camera_id', cameraId);

  addLog(`Uploading "${file.name}" to CAM_0${cameraId} …`, 'info');

  try {
    const res = await fetch('/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (data.success) {
      addLog(`CAM_0${cameraId} streaming: ${file.name}`, 'info');
      loadFeed(cameraId);
    } else {
      addLog(`Upload failed: ${data.error || 'unknown error'}`, 'danger');
    }
  } catch (err) {
    addLog(`Upload error on CAM_0${cameraId}: ${err.message}`, 'danger');
  }
}

function openIPModal(cameraId) {
  closeAllMenus();
  document.getElementById('modal-cam-id').value = cameraId;
  const ipInput = document.getElementById('modal-ip');
  ipInput.value = '';
  ipInput.classList.remove('error');
  const errorEl = document.getElementById('modal-ip-error');
  if (errorEl) errorEl.classList.remove('visible');
  document.getElementById('modal-backdrop').classList.remove('hidden');
  setTimeout(() => ipInput.focus(), 50);
}

function closeModal() {
  document.getElementById('modal-backdrop').classList.add('hidden');
}

async function connectCamera() {
  const cameraId = parseInt(document.getElementById('modal-cam-id').value, 10);
  const ipInput  = document.getElementById('modal-ip');
  const errorEl  = document.getElementById('modal-ip-error');
  const ip       = ipInput.value.trim();

  // Inline validation
  if (!ip) {
    ipInput.classList.add('error');
    if (errorEl) { errorEl.textContent = 'Please enter a stream URL or IP address.'; errorEl.classList.add('visible'); }
    ipInput.focus();
    return;
  }
  ipInput.classList.remove('error');
  if (errorEl) errorEl.classList.remove('visible');

  closeModal();
  addLog(`Connecting CAM_0${cameraId} → ${ip}`, 'info');

  try {
    const res  = await fetch('/set_ip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: cameraId, ip }),
    });
    const data = await res.json();
    if (data.success) {
      addLog(`CAM_0${cameraId} connected to IP stream.`, 'info');
      loadFeed(cameraId);
    } else {
      addLog(`Connection failed: ${data.error}`, 'danger');
    }
  } catch (err) {
    addLog(`Connection error: ${err.message}`, 'danger');
  }
}

async function closeCamera(cameraId) {
  closeAllMenus();
  try {
    const res  = await fetch(`/close_camera/${cameraId}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      addLog(`CAM_0${cameraId} disconnected.`, 'info');
      // Reload blank feed
      loadFeed(cameraId);
    }
  } catch (err) {
    addLog(`Failed to disconnect CAM_0${cameraId}: ${err.message}`, 'danger');
  }
}

async function resetAlert(cameraId) {
  try {
    const res  = await fetch(`/reset_alert/${cameraId}`, { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      addLog(`Alerts dismissed for CAM_0${cameraId}.`, 'info');
      if (prevState[cameraId]) {
        prevState[cameraId].fall  = false;
        prevState[cameraId].theft = false;
        prevState[cameraId].fire  = false;
      }
      document.getElementById(`alert-${cameraId}`)?.classList.add('hidden');
      document.getElementById(`theft-${cameraId}`)?.classList.add('hidden');
      document.getElementById(`fire-${cameraId}`)?.classList.add('hidden');
      document.getElementById(`reset-${cameraId}`)?.classList.add('hidden');
    }
  } catch (err) {
    addLog(`Could not reset alerts: ${err.message}`, 'danger');
  }
}

// ── Alert log ──────────────────────────────────────────────────────────────────
function addLog(message, level = 'info') {
  const container = document.getElementById('alerts-log');
  if (!container) return;

  // Build only the new entry element and prepend — no full re-render
  const el = document.createElement('div');
  el.className = `log-entry log-${level} is-new`;
  el.innerHTML = `
    <span class="log-time">${now()}</span>
    <span class="log-tag ${level}">${level.toUpperCase()}</span>
    <span class="log-msg">${escHtml(message)}</span>
  `;
  container.prepend(el);

  // Remove animation class after it completes so it doesn't re-trigger
  setTimeout(() => el.classList.remove('is-new'), 400);

  // Keep log to 50 entries max
  while (container.children.length > 50) {
    container.removeChild(container.lastChild);
  }
}

function clearAlerts() {
  const container = document.getElementById('alerts-log');
  if (!container) return;
  container.innerHTML = '';
  const el = document.createElement('div');
  el.className = 'log-entry log-info';
  el.innerHTML = `
    <span class="log-time">${now()}</span>
    <span class="log-tag info">INFO</span>
    <span class="log-msg">Event log cleared.</span>
  `;
  container.appendChild(el);
}

function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}