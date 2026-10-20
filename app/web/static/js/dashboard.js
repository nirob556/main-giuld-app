// dashboard.js — ZBR FF GLORY SPA (production build)
const State = {
  view: 'dashboard',
  clients: [],
  detailClientId: null,
  myCodes: [],
  stats: { glory_count: 0, total_clients: 0, running_clients: 0, total_accounts: 0, online_accounts: 0, in_match_accounts: 0, balance: 0 },
  user: null,
  ws: null,
  wsRetries: 0,
  suspendSoftRefresh: false,
  _clientsFingerprint: null,
};

const content   = document.getElementById('content');
const viewTitle = document.getElementById('viewTitle');
const viewSub   = document.getElementById('viewSub');

/* =========================================================
   THEME
   ========================================================= */
(function initTheme() {
  const saved = localStorage.getItem('zbr_theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  const sw = document.getElementById('themeSwitch');
  if (saved === 'light') sw.classList.add('on');

  sw.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('zbr_theme', next);
    sw.classList.toggle('on', next === 'light');
  });
})();

/* =========================================================
   NAVIGATION
   ========================================================= */
document.getElementById('navList').addEventListener('click', (e) => {
  const item = e.target.closest('.nav-item');
  if (!item || !item.dataset.view) return;
  setView(item.dataset.view);
  closeDrawer();
});

document.getElementById('tabBar').addEventListener('click', (e) => {
  const item = e.target.closest('.tab-item');
  if (!item) return;
  setView(item.dataset.view);
});

document.getElementById('logoutBtn').addEventListener('click', async () => {
  try {
    await Api.post('/api/auth/logout');
  } catch { /* ignore */ }
  window.location.href = '/login';
});

/* =========================================================
   MOBILE DRAWER
   ========================================================= */
const sidebarEl = document.getElementById('sidebar');
const sidebarBackdrop = document.getElementById('sidebarBackdrop');
function openDrawer()  { sidebarEl.classList.add('open');  sidebarBackdrop.classList.add('open'); }
function closeDrawer() { sidebarEl.classList.remove('open'); sidebarBackdrop.classList.remove('open'); }
document.getElementById('menuBtn').addEventListener('click', openDrawer);
sidebarBackdrop.addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeDrawer(); closeCreateModal(); closeCreateCodeModal(); }
});

/* =========================================================
   VIEW META
   ========================================================= */
const VIEW_META = {
  dashboard: ['Dashboard', 'Overview of all squad operations'],
  clients:   ['Clients / Squads', 'Manage every squad you are running'],
  redeem:    ['Redeem Codes', 'Create or claim a balance code'],
  settings:  ['Settings', 'Defaults for new clients'],
  detail:    ['Squad', 'Loading…'],
};

function setView(view, params = {}) {
  State.view = view;
  if (view === 'detail') State.detailClientId = params.clientId;

  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));
  document.querySelectorAll('.tab-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));

  const meta = VIEW_META[view] || ['', ''];
  viewTitle.textContent = meta[0];
  viewSub.textContent = meta[1];
  render();
}

/* =========================================================
   ICONS
   ========================================================= */
const Icons = {
  clients: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
  bolt: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h8l-1 8 10-12h-8l1-8z"/></svg>',
  pulse: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
  gamepad: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="11" rx="4"/><path d="M7 10v4M5 12h4"/><circle cx="16" cy="11" r="1"/><circle cx="18.5" cy="14" r="1"/></svg>',
  archive: '<svg class="icon icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v9a2 2 0 002 2h10a2 2 0 002-2V9"/><path d="M10 13h4"/></svg>',
  warning: '<svg class="icon icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9L1.8 18a1.5 1.5 0 001.3 2.3h17.8a1.5 1.5 0 001.3-2.3L13.7 3.9a1.5 1.5 0 00-2.6 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
  handshake: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 13l3 3 7-7"/><path d="M2 12l5-5 4 2 4-2 5 5"/><path d="M11 10l-4 4-3-3"/></svg>',
  bot: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="7" r="4"/></svg>',
  arrowRight: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>',
  ticket: '<svg class="icon icon-lg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M12 6v12"/></svg>',
  copy: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>',
  check: '<svg class="icon icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
};

function statIconChip(svg) { return `<span class="icon-chip">${svg}</span>`; }

/* =========================================================
   RENDER ROUTER
   ========================================================= */
function render() {
  if (State.view === 'dashboard') renderDashboard();
  else if (State.view === 'clients')   renderClients();
  else if (State.view === 'detail')    renderDetail();
  else if (State.view === 'redeem')    renderRedeem();
  else if (State.view === 'settings')  renderSettings();
  renderAnnouncementBanner();
}

/* =========================================================
   DASHBOARD
   ========================================================= */
function statCard(label, value, iconSvg) {
  return `<div class="card stat-card">
    <div class="label">${statIconChip(iconSvg)}${label}</div>
    <div class="value">${value}</div>
  </div>`;
}

function renderDashboard() {
  const s = State.stats;
  content.innerHTML = `
    <div class="grid grid-4">
      ${statCard('Wallet Balance', '$' + s.balance.toFixed(2), Icons.bolt)}
      ${statCard('Total Clients',   s.total_clients,   Icons.clients)}
      ${statCard('Online Accounts', s.online_accounts, Icons.pulse)}
      ${statCard('In Match',        s.in_match_accounts, Icons.gamepad)}
    </div>
    <div class="section-head">
      <h2>Recent Clients</h2>
      <a href="#" id="viewAllClients" class="section-link">View all ${Icons.arrowRight}</a>
    </div>
    <div class="grid grid-3" id="recentClientsGrid"></div>
  `;
  document.getElementById('viewAllClients').addEventListener('click', (e) => {
    e.preventDefault();
    setView('clients');
  });
  renderClientCards(document.getElementById('recentClientsGrid'), State.clients.slice(0, 6));
}

/* =========================================================
   CLIENTS
   ========================================================= */
function renderClients() {
  content.innerHTML = `<div class="grid grid-3" id="clientsGrid"></div>`;
  renderClientCards(document.getElementById('clientsGrid'), State.clients);
}

function renderClientCards(container, clients) {
  if (!clients.length) {
    container.innerHTML = `<div class="empty-state" style="grid-column:1/-1;">
      <div class="icon-wrap">${Icons.archive}</div>
      <div class="title">No clients yet</div>
      <div class="sub">Click <b>New Client</b> to spin up your first squad. Each squad costs $1.00.</div>
    </div>`;
    return;
  }
  container.innerHTML = clients.map(c => {
    const slotCount = c.bot_count || 4;
    const slotsHtml = Array.from({ length: slotCount }).map((_, i) => {
      const slot = (c._slots || [])[i];
      const cls = slot ? slot.ui_state : '';
      return `<div class="slot-dot ${cls}"></div>`;
    }).join('');
    const modeIcon  = c.mode === 'with_me' ? Icons.handshake : Icons.bot;
    const modeLabel = c.mode === 'with_me' ? 'Play with me' : 'Normal squad';
    return `
    <div class="squad-card" data-client-id="${c.id}">
      <div class="squad-top">
        <div>
          <div class="squad-name">${escapeHtml(c.name)}</div>
          <div class="squad-mode">${modeIcon}${modeLabel} · ${slotCount} slots</div>
        </div>
        <span class="badge ${c.live_running ? 'running' : 'stopped'}">${c.live_running ? 'RUNNING' : 'STOPPED'}</span>
      </div>
      <div class="slot-row">${slotsHtml}</div>
      <div class="squad-meta">
        <span>${c.online_count || 0}/${slotCount} online</span>
        <span>${c.clan_id ? 'Clan: ' + escapeHtml(c.clan_id) : 'No clan'}</span>
      </div>
    </div>`;
  }).join('');

  container.querySelectorAll('.squad-card').forEach(card => {
    card.addEventListener('click', () => setView('detail', { clientId: card.dataset.clientId }));
  });
}

/* =========================================================
   DETAIL — shows player nickname + matches played per slot
   ========================================================= */
async function renderDetail() {
  const id = State.detailClientId;
  let client = State.clients.find(c => c.id === id);

  content.innerHTML = `<div class="skeleton" style="height:220px;border-radius:16px;"></div>`;

  try {
    const [cRes, sRes] = await Promise.all([
      Api.get(`/api/clients/${id}`),
      Api.get(`/api/clients/${id}/slots`),
    ]);
    client = cRes.client;
    const slots = sRes.slots;

    viewTitle.textContent = client.name;
    viewSub.textContent = client.mode === 'with_me' ? 'Play with me squad' : 'Normal squad';

    const slotCount = client.bot_count || 4;
    const slotCards = Array.from({ length: slotCount }).map((_, i) => {
      const acc = slots[i];
      if (!acc) {
        return `<div class="card">
          <div class="muted" style="font-size:12.5px;">Slot ${i + 1} — not assigned</div>
        </div>`;
      }
      const stateLabel = {
        filling: 'Filling / Joining', standing: 'Standing By', in_match: 'In Match',
        connecting: 'Connecting', error: 'Error',
      }[acc.ui_state] || acc.ui_state;
      const dotClass = {
        filling: 'connecting', standing: 'online', in_match: 'online',
        connecting: 'connecting', error: 'error',
      }[acc.ui_state] || 'offline';

      return `
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
          <div style="min-width:0;">
            <div style="font-weight:700;font-size:13.5px;">Slot ${acc.slot || i + 1}${acc.role === 'bot' ? '' : ' (You)'}</div>
            <div class="muted" style="font-size:12px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(acc.nickname || acc.uid)}</div>
          </div>
          <span class="pill"><span class="status-dot ${dotClass}"></span>${stateLabel}</span>
        </div>
        <div style="margin-top:14px;font-size:12px;color:var(--text-dim);display:flex;justify-content:space-between;">
          <span>UID: ${escapeHtml(acc.uid)}</span>
          <span>Lvl ${acc.level || 1}</span>
        </div>
        <div style="margin-top:4px;font-size:12px;color:var(--text-dim);">Matches played: ${acc.matches_played || 0}</div>
      </div>`;
    }).join('');

    content.innerHTML = `
      <div class="card" style="margin-bottom:24px;">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;">
          <div style="display:flex;gap:24px;flex-wrap:wrap;">
            <div><div class="muted" style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;">Mode</div><div style="font-weight:700;font-size:13.5px;margin-top:2px;">${client.mode === 'with_me' ? 'Play with me' : 'Normal'}</div></div>
            <div><div class="muted" style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;">Captain UID</div><div style="font-weight:700;font-size:13.5px;margin-top:2px;">${escapeHtml(client.captain_uid || '—')}</div></div>
            <div><div class="muted" style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;">Clan</div><div style="font-weight:700;font-size:13.5px;margin-top:2px;">${escapeHtml(client.clan_id || '—')}</div></div>
            <div><div class="muted" style="font-size:11px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;">Status</div><div style="margin-top:2px;"><span class="badge ${client.live_running ? 'running' : 'stopped'}">${client.live_running ? 'RUNNING' : 'STOPPED'}</span></div></div>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            ${client.live_running
              ? `<button class="btn btn-sm" id="stopBtn">Stop</button>
                 <button class="btn btn-sm" id="restartBtn">Restart</button>`
              : `<button class="btn btn-sm btn-accent" id="startBtn">Start</button>`}
            <button class="btn btn-sm btn-danger" id="deleteBtn">Delete</button>
          </div>
        </div>
        ${client.notes ? `<div class="muted" style="margin-top:14px;font-size:13px;padding-top:14px;border-top:1px solid var(--border-soft);">${escapeHtml(client.notes)}</div>` : ''}
      </div>
      <div class="section-head"><h2>Squad Slots — Players &amp; Matches</h2></div>
      <div class="grid grid-4">${slotCards}</div>
    `;

    bindDetailActions(id, client);
  } catch (e) {
    content.innerHTML = `<div class="empty-state">
      <div class="icon-wrap">${Icons.warning}</div>
      <div class="title">Could not load this client</div>
      <div class="sub">${escapeHtml(e.message)}</div>
    </div>`;
  }
}

function bindDetailActions(id, client) {
  const startBtn = document.getElementById('startBtn');
  if (startBtn) startBtn.addEventListener('click', async () => {
    startBtn.disabled = true;
    startBtn.innerHTML = '<span class="spinner"></span>';
    State.suspendSoftRefresh = true;
    try {
      await Api.post(`/api/clients/${id}/start`);
      Toast.success('Client starting…');
      await refreshClients();
      renderDetail();
    } catch (e) {
      Toast.error(e.message);
      startBtn.disabled = false;
      startBtn.textContent = 'Start';
    } finally {
      State.suspendSoftRefresh = false;
    }
  });

  const stopBtn = document.getElementById('stopBtn');
  if (stopBtn) stopBtn.addEventListener('click', async () => {
    stopBtn.disabled = true;
    State.suspendSoftRefresh = true;
    try {
      await Api.post(`/api/clients/${id}/stop`);
      Toast.warning('Client stopping…');
      await refreshClients();
      renderDetail();
    } catch (e) {
      Toast.error(e.message);
      stopBtn.disabled = false;
    } finally {
      State.suspendSoftRefresh = false;
    }
  });

  const restartBtn = document.getElementById('restartBtn');
  if (restartBtn) restartBtn.addEventListener('click', async () => {
    restartBtn.disabled = true;
    State.suspendSoftRefresh = true;
    try {
      await Api.post(`/api/clients/${id}/restart`);
      Toast.info('Restarting…');
      await refreshClients();
      renderDetail();
    } catch (e) {
      Toast.error(e.message);
      restartBtn.disabled = false;
    } finally {
      State.suspendSoftRefresh = false;
    }
  });

  const deleteBtn = document.getElementById('deleteBtn');
  if (deleteBtn) deleteBtn.addEventListener('click', async () => {
    if (deleteBtn.dataset.busy === '1') return; // guard double-click / double-fire
    if (!confirm(`Delete "${client.name}"? This stops and removes the squad permanently.`)) return;

    deleteBtn.dataset.busy = '1';
    deleteBtn.disabled = true;
    deleteBtn.innerHTML = '<span class="spinner"></span> Deleting…';
    State.suspendSoftRefresh = true; // don't let a WS push re-render detail mid-delete

    try {
      await Api.del(`/api/clients/${id}`, { timeout: 30000 });
      Toast.success('Client deleted');
      State.suspendSoftRefresh = false;
      await refreshClients();
      setView('clients');
    } catch (e) {
      State.suspendSoftRefresh = false;
      Toast.error(e.message || 'Could not delete this client');
      deleteBtn.dataset.busy = '0';
      deleteBtn.disabled = false;
      deleteBtn.textContent = 'Delete';
    }
  });
}

/* =========================================================
   REDEEM CODES
   ========================================================= */
async function renderRedeem() {
  content.innerHTML = `
    <div class="grid grid-2" style="align-items:start;">
      <div class="card">
        <div style="font-weight:700;font-size:14.5px;margin-bottom:4px;">Claim a code</div>
        <div class="muted" style="font-size:12.5px;margin-bottom:16px;">Enter a redeem code to add its value to your balance.</div>
        <form id="claimCodeForm">
          <div class="form-row">
            <label for="f_claim_code">Redeem code</label>
            <input type="text" id="f_claim_code" placeholder="ZBR-XXXX-XXXX-XXXX" style="text-transform:uppercase;letter-spacing:.5px;" required>
          </div>
          <button type="submit" class="btn btn-accent btn-block" id="claimCodeBtn">Redeem</button>
        </form>
      </div>
      <div class="card">
        <div style="font-weight:700;font-size:14.5px;margin-bottom:4px;">Create a code</div>
        <div class="muted" style="font-size:12.5px;margin-bottom:16px;">Lock part of your balance into a code you can share. Current balance: <b id="redeemBalanceLabel">$${State.stats.balance.toFixed(2)}</b></div>
        <button class="btn btn-accent btn-block" id="openCreateCodeBtn">
          ${Icons.ticket.replace('icon-lg', 'icon')}
          Generate Redeem Code
        </button>
      </div>
    </div>
    <div class="section-head"><h2>Your Codes</h2></div>
    <div id="myCodesWrap"></div>
  `;

  document.getElementById('openCreateCodeBtn').addEventListener('click', openCreateCodeModal);

  document.getElementById('claimCodeForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = document.getElementById('claimCodeBtn');
    const input = document.getElementById('f_claim_code');
    const code = input.value.trim();
    if (!code) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Redeeming…';
    try {
      const res = await Api.post('/api/redeem/claim', { code });
      Toast.success(`Redeemed $${res.credited.toFixed(2)} — new balance $${res.new_balance.toFixed(2)}`);
      input.value = '';
      await Promise.all([refreshStats(), refreshMyCodes()]);
      renderRedeem();
    } catch (err) {
      Toast.error(err.message || 'Could not redeem this code');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Redeem';
    }
  });

  await refreshMyCodes();
  renderMyCodes();
}

function renderMyCodes() {
  const wrap = document.getElementById('myCodesWrap');
  if (!wrap) return;
  if (!State.myCodes.length) {
    wrap.innerHTML = `<div class="empty-state">
      <div class="icon-wrap">${Icons.ticket}</div>
      <div class="title">No codes yet</div>
      <div class="sub">Codes you generate will show here, along with who redeems them.</div>
    </div>`;
    return;
  }
  wrap.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Code</th><th>Value</th><th>Status</th><th>Created</th></tr></thead>
    <tbody>
      ${State.myCodes.map(c => `
        <tr>
          <td style="font-family:'SF Mono',Consolas,monospace;font-size:12px;">${escapeHtml(c.code)}</td>
          <td>$${c.value.toFixed(2)}</td>
          <td>${c.is_redeemed
            ? `<span class="pill">${Icons.check} Redeemed by ${escapeHtml(c.redeemed_by_username || 'someone')}</span>`
            : `<span class="muted">Not redeemed yet</span>`}</td>
          <td class="muted">${new Date(c.created_at * 1000).toLocaleDateString()}</td>
        </tr>
      `).join('')}
    </tbody>
  </table></div>`;
}

async function refreshMyCodes() {
  try {
    const res = await Api.get('/api/redeem/mine');
    State.myCodes = res.codes || [];
  } catch (e) {
    console.warn('codes refresh failed:', e.message);
  }
}

/* ---- create-code modal ---- */
const createCodeBackdrop = document.getElementById('createCodeModalBackdrop');
const createCodeForm     = document.getElementById('createCodeForm');

function openCreateCodeModal() {
  createCodeForm.reset();
  createCodeBackdrop.classList.add('open');
  setTimeout(() => document.getElementById('f_code_amount').focus(), 100);
}
function closeCreateCodeModal() {
  createCodeBackdrop.classList.remove('open');
}
document.getElementById('closeCreateCodeModal').addEventListener('click', closeCreateCodeModal);
document.getElementById('cancelCreateCodeBtn').addEventListener('click', closeCreateCodeModal);
createCodeBackdrop.addEventListener('click', (e) => {
  if (e.target === createCodeBackdrop) closeCreateCodeModal();
});

createCodeForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submitCreateCodeBtn');
  const amount = parseFloat(document.getElementById('f_code_amount').value);
  if (!amount || amount <= 0) {
    Toast.error('Enter a valid amount');
    return;
  }
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Generating…';
  try {
    const res = await Api.post('/api/redeem/create', { amount });
    closeCreateCodeModal();
    Toast.success(`Code created: ${res.code.code}`);
    await Promise.all([refreshStats(), refreshMyCodes()]);
    render();
  } catch (err) {
    Toast.error(err.message || 'Could not create code');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Generate Code';
  }
});

/* =========================================================
   SETTINGS
   ========================================================= */
function renderSettings() {
  content.innerHTML = `
    <div class="card" style="max-width:580px;">
      <div style="font-weight:700;font-size:15px;margin-bottom:6px;">About ZBR FF GLORY</div>
      <div class="muted" style="font-size:13.5px;line-height:1.7;">
        This console drives the squad engine directly — client creation triggers the same
        account generation / guest login / team-invite flow that used to run through the
        terminal menu. Each squad costs $1.00 from your wallet balance. Defaults for new
        clients (bot name prefix, default clan) can be set per-client at creation time in
        the <b>New Client</b> dialog.
      </div>
      <div style="margin-top:20px;display:flex;gap:10px;flex-wrap:wrap;">
        <span class="pill">Engine: Garena FF protocol stack</span>
        <span class="pill">Mode: Battle Royale only</span>
      </div>
    </div>
  `;
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/* =========================================================
   DATA FETCH
   ========================================================= */
async function refreshStats() {
  try {
    const res = await Api.get('/api/stats');
    State.stats = res.stats;
    const amt = document.getElementById('balanceAmount');
    if (amt) amt.textContent = '$' + res.stats.balance.toFixed(2);
  } catch (e) {
    console.warn('stats refresh failed:', e.message);
  }
}

async function refreshMe() {
  try {
    const res = await Api.get('/api/auth/me');
    State.user = res.user;
    if (res.user.role === 'admin') {
      document.getElementById('adminNavLink').classList.remove('hidden');
    }
  } catch (e) {
    console.warn('me refresh failed:', e.message);
  }
}

function _clientsFingerprint(clients) {
  // cheap signature of everything the cards actually render, so a soft
  // refresh can skip the DOM rebuild (and the visual jump/flicker that
  // comes with it) when nothing the user can see has actually changed.
  return JSON.stringify(clients.map(c => [
    c.id, c.name, c.live_running, c.online_count, c.clan_id,
    (c._slots || []).map(s => s && s.ui_state),
  ]));
}

async function refreshClients() {
  try {
    const res = await Api.get('/api/clients');
    const clients = res.clients || [];

    await Promise.all(clients.map(async c => {
      try {
        const s = await Api.get(`/api/clients/${c.id}/slots`);
        c._slots = s.slots || [];
      } catch {
        c._slots = [];
      }
    }));

    const nextFp = _clientsFingerprint(clients);
    const changed = nextFp !== State._clientsFingerprint;
    State._clientsFingerprint = nextFp;
    State.clients = clients;
    return changed;
  } catch (e) {
    console.warn('clients refresh failed:', e.message);
    return false;
  }
}

/* =========================================================
   CREATE CLIENT MODAL
   ========================================================= */
const createBackdrop = document.getElementById('createModalBackdrop');
const createForm     = document.getElementById('createClientForm');
let selectedMode     = 'normal';

function openCreateModal() {
  createForm.reset();
  selectedMode = 'normal';
  document.querySelectorAll('.mode-opt').forEach(o =>
    o.classList.toggle('selected', o.dataset.mode === 'normal')
  );
  document.getElementById('captainRow').classList.add('hidden');
  document.getElementById('f_prefix').value = 'ZIBON_';
  createBackdrop.classList.add('open');
  setTimeout(() => document.getElementById('f_name').focus(), 100);
}
function closeCreateModal() {
  createBackdrop.classList.remove('open');
}

document.getElementById('newClientBtn').addEventListener('click', openCreateModal);
document.getElementById('closeCreateModal').addEventListener('click', closeCreateModal);
document.getElementById('cancelCreateBtn').addEventListener('click', closeCreateModal);
createBackdrop.addEventListener('click', (e) => {
  if (e.target === createBackdrop) closeCreateModal();
});

document.querySelectorAll('.mode-opt').forEach(opt => {
  const activate = () => {
    selectedMode = opt.dataset.mode;
    document.querySelectorAll('.mode-opt').forEach(o =>
      o.classList.toggle('selected', o === opt)
    );
    document.getElementById('captainRow').classList.toggle('hidden', selectedMode !== 'with_me');
  };
  opt.addEventListener('click', activate);
  opt.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
  });
});

createForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('submitCreateBtn');
  const name = document.getElementById('f_name').value.trim();
  const captainUid = document.getElementById('f_captain_uid').value.trim();

  if (!name) {
    Toast.error('Client name is required');
    return;
  }
  if (selectedMode === 'with_me' && !/^\d+$/.test(captainUid)) {
    Toast.error('Enter a valid numeric game UID for "Play with me" mode');
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Creating…';

  try {
    await Api.post('/api/clients', {
      name,
      mode: selectedMode,
      captain_uid: captainUid,
      clan_id: document.getElementById('f_clan_id').value.trim(),
      name_prefix: document.getElementById('f_prefix').value.trim() || 'ZIBON_',
      notes: document.getElementById('f_notes').value.trim(),
      auto_start: true,
    });
    Toast.success('Client created — bots are connecting now');
    closeCreateModal();
    await Promise.all([refreshClients(), refreshStats()]);
    render();
  } catch (err) {
    Toast.error(err.message || 'Failed to create client');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Create & Start';
  }
});

/* =========================================================
   ADD BALANCE — USD stepper, BDT convert, ZR Pay BD handoff,
   server-verified status poll (never trusts the redirect itself)
   ========================================================= */
const PaymentState = {
  config: { exchange_rate: 110, min_deposit_usd: 1, max_deposit_usd: 500 },
  pollTimer: null,
  pollDeposit: null,
};

async function loadPaymentConfig() {
  try {
    const res = await Api.get('/api/payments/config');
    PaymentState.config = res;
    const { exchange_rate, min_deposit_usd, max_deposit_usd } = res;
    document.getElementById('depositRateLabel').textContent = `1 USD = ${exchange_rate} BDT`;
    document.getElementById('depositLimitHint').lastChild.textContent =
      ` Min $${min_deposit_usd.toFixed(2)} · Max $${max_deposit_usd.toFixed(2)}`;
    const input = document.getElementById('f_deposit_amount');
    input.min = min_deposit_usd;
    input.max = max_deposit_usd;
  } catch (e) { console.warn('payment config refresh failed:', e.message); }
}

function updateDepositTotal() {
  const amount = parseFloat(document.getElementById('f_deposit_amount').value) || 0;
  const bdt = amount * PaymentState.config.exchange_rate;
  document.getElementById('depositBdtTotal').textContent = '৳' + bdt.toFixed(2);
}

const addBalanceBackdrop = document.getElementById('addBalanceModalBackdrop');
function openAddBalanceModal() {
  document.getElementById('f_deposit_amount').value = Math.max(1, Math.round(PaymentState.config.min_deposit_usd));
  updateDepositTotal();
  addBalanceBackdrop.classList.add('open');
  loadPaymentConfig().then(updateDepositTotal);
}
function closeAddBalanceModal() { addBalanceBackdrop.classList.remove('open'); }

document.getElementById('addBalanceBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  openAddBalanceModal();
});
document.getElementById('closeAddBalanceModal').addEventListener('click', closeAddBalanceModal);
document.getElementById('cancelAddBalanceBtn').addEventListener('click', closeAddBalanceModal);
addBalanceBackdrop.addEventListener('click', (e) => { if (e.target === addBalanceBackdrop) closeAddBalanceModal(); });

const depositAmountInput = document.getElementById('f_deposit_amount');
depositAmountInput.addEventListener('input', updateDepositTotal);

document.getElementById('amountDecBtn').addEventListener('click', () => {
  const cur = parseFloat(depositAmountInput.value) || 0;
  const next = Math.max(PaymentState.config.min_deposit_usd, cur - 1);
  depositAmountInput.value = next;
  updateDepositTotal();
});
document.getElementById('amountIncBtn').addEventListener('click', () => {
  const cur = parseFloat(depositAmountInput.value) || 0;
  const next = Math.min(PaymentState.config.max_deposit_usd, cur + 1);
  depositAmountInput.value = next;
  updateDepositTotal();
});

let confirmInFlight = false; // hard guard against duplicate-click double POST
document.getElementById('confirmPaymentBtn').addEventListener('click', async () => {
  if (confirmInFlight) return;
  const btn = document.getElementById('confirmPaymentBtn');
  const amount = parseFloat(depositAmountInput.value);
  const { min_deposit_usd, max_deposit_usd } = PaymentState.config;

  if (!amount || amount < min_deposit_usd || amount > max_deposit_usd) {
    Toast.error(`Enter an amount between $${min_deposit_usd.toFixed(2)} and $${max_deposit_usd.toFixed(2)}`);
    return;
  }

  confirmInFlight = true;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Creating payment…';

  try {
    const res = await Api.post('/api/payments/deposits', { amount_usd: amount }, { timeout: 30000 });
    const deposit = res.deposit;
    closeAddBalanceModal();
    if (deposit.payment_url) {
      window.open(deposit.payment_url, '_blank', 'noopener');
    }
    openPaymentStatusModal(deposit.id);
  } catch (err) {
    Toast.error(err.message || 'Could not start payment');
  } finally {
    confirmInFlight = false;
    btn.disabled = false;
    btn.textContent = 'Confirm Payment';
  }
});

/* ---- payment status modal: polls server-verified deposit status ---- */
const paymentStatusBackdrop = document.getElementById('paymentStatusModalBackdrop');
const paymentStatusTitle = document.getElementById('paymentStatusTitle');
const paymentStatusSub = document.getElementById('paymentStatusSub');
const paymentStatusBody = document.getElementById('paymentStatusBody');

function openPaymentStatusModal(depositId) {
  PaymentState.pollDeposit = depositId;
  paymentStatusBody.innerHTML = `
    <div class="spinner" style="width:28px;height:28px;margin:0 auto 16px;"></div>
    <h3 id="paymentStatusTitle">Checking payment…</h3>
    <div class="muted" id="paymentStatusSub" style="margin-top:6px;font-size:13px;">
      Waiting for you to complete payment in the new tab. We verify directly with the gateway — this updates automatically.
    </div>`;
  paymentStatusBackdrop.classList.add('open');
  pollDepositStatus();
}
function closePaymentStatusModal() {
  paymentStatusBackdrop.classList.remove('open');
  if (PaymentState.pollTimer) { clearTimeout(PaymentState.pollTimer); PaymentState.pollTimer = null; }
}
document.getElementById('closePaymentStatusBtn').addEventListener('click', closePaymentStatusModal);

async function pollDepositStatus() {
  const id = PaymentState.pollDeposit;
  if (!id) return;
  try {
    const res = await Api.get(`/api/payments/deposits/${id}`);
    const d = res.deposit;
    renderPaymentStatus(d);
    if (d.status === 'pending' || d.status === 'verifying') {
      PaymentState.pollTimer = setTimeout(pollDepositStatus, 3000);
    } else {
      await Promise.all([refreshStats(), refreshMyCodes().catch(() => {})]);
    }
  } catch (e) {
    PaymentState.pollTimer = setTimeout(pollDepositStatus, 4000);
  }
}

function renderPaymentStatus(d) {
  const body = document.getElementById('paymentStatusBody');
  if (!body) return;
  if (d.status === 'success') {
    body.innerHTML = `
      <div class="icon-wrap" style="color:var(--green);margin:0 auto 12px;">${Icons.check}</div>
      <h3>Payment successful</h3>
      <div class="muted" style="margin-top:6px;font-size:13px;">$${d.amount_usd.toFixed(2)} added to your balance.</div>`;
  } else if (d.status === 'failed') {
    body.innerHTML = `
      <div class="icon-wrap" style="color:var(--red);margin:0 auto 12px;">${Icons.warning}</div>
      <h3>Payment not completed</h3>
      <div class="muted" style="margin-top:6px;font-size:13px;">${escapeHtml(d.failure_reason || 'The payment could not be verified. No balance was added.')}</div>`;
  } else {
    body.innerHTML = `
      <div class="spinner" style="width:28px;height:28px;margin:0 auto 16px;"></div>
      <h3>Checking payment…</h3>
      <div class="muted" style="margin-top:6px;font-size:13px;">Still waiting for gateway confirmation.</div>`;
  }
}

/* =========================================================
   WEBSOCKET LIVE FEED (account/slot updates only — no logs)
   ========================================================= */
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws/live`);
  } catch {
    setTimeout(connectWs, 3000);
    return;
  }
  State.ws = ws;

  ws.onopen = () => { State.wsRetries = 0; };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'account' || msg.type === 'account_removed') {
      scheduleSoftRefresh();
    }
  };

  ws.onclose = () => {
    State.wsRetries = Math.min(State.wsRetries + 1, 10);
    const delay = Math.min(2000 * State.wsRetries, 15000);
    setTimeout(connectWs, delay);
  };
  ws.onerror = () => ws.close();
}

function anyModalOpen() {
  return document.querySelector('.modal-backdrop.open') !== null;
}

let softRefreshTimer = null;
function scheduleSoftRefresh() {
  if (softRefreshTimer) return;
  softRefreshTimer = setTimeout(async () => {
    softRefreshTimer = null;
    if (State.suspendSoftRefresh || anyModalOpen()) {
      // don't yank the UI out from under an in-flight action (delete/start/
      // stop/payment) or while a modal is open — try again shortly instead
      // of silently dropping the update.
      scheduleSoftRefresh();
      return;
    }
    await refreshStats();
    if (State.view === 'detail') {
      renderDetail();
    } else if (State.view === 'clients' || State.view === 'dashboard') {
      const changed = await refreshClients();
      if (changed) render();
    }
  }, 1500);
}

/* =========================================================
   BOOT
   ========================================================= */
(async function boot() {
  await Promise.all([refreshMe(), refreshStats(), refreshClients(), loadPaymentConfig(), loadAnnouncement()]);
  render();
  connectWs();
  setInterval(refreshStats, 15000);

  // returning from the gateway redirect (/api/payments/return/{id} -> here)
  const params = new URLSearchParams(window.location.search);
  const depositId = params.get('deposit');
  if (depositId) {
    openPaymentStatusModal(depositId);
    const cleanUrl = window.location.pathname + window.location.hash;
    window.history.replaceState({}, '', cleanUrl);
  }
})();

/* =========================================================
   ANNOUNCEMENT BANNER — rendered above content, set from admin panel
   ========================================================= */
State.announcement = null;
async function loadAnnouncement() {
  try {
    const res = await Api.get('/api/payments/announcement');
    State.announcement = res;
    renderAnnouncementBanner();
  } catch { /* non-fatal — banner just doesn't show */ }
}

function renderAnnouncementBanner() {
  const existing = document.getElementById('announcementBanner');
  if (existing) existing.remove();
  const a = State.announcement;
  if (!a) return;
  const text = a.maintenance_mode
    ? (a.maintenance_message || 'The site is temporarily in maintenance mode.')
    : (a.announcement || '').trim();
  if (!text) return;
  const banner = document.createElement('div');
  banner.id = 'announcementBanner';
  banner.className = 'announcement-banner';
  banner.innerHTML = `${Icons.bolt}<span>${escapeHtml(text)}</span>`;
  content.prepend(banner);
}
