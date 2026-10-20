// admin.js — ZBR FF GLORY admin panel SPA
const State = {
  view: 'dashboard',
  users: [],
  usersTotal: 0,
  usersPage: 0,
  usersSearch: '',
  clients: [],
  codes: [],
  deposits: [],
  depositsTotal: 0,
  depositsPage: 0,
  depositsFilter: 'all',
  depositsSearch: '',
  auditEntries: [],
  auditTotal: 0,
  auditPage: 0,
  dashboardStats: null,
  settings: null,
  targetUserId: null,
  targetUsername: '',
};

const PAGE_SIZE = 25;

const content   = document.getElementById('content');
const viewTitle = document.getElementById('viewTitle');
const viewSub   = document.getElementById('viewSub');

/* theme (shared pattern with dashboard.js) */
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

/* nav */
document.getElementById('navList').addEventListener('click', (e) => {
  const item = e.target.closest('.nav-item');
  if (!item) return;
  setView(item.dataset.view);
  closeDrawer();
});
document.getElementById('tabBar').addEventListener('click', (e) => {
  const item = e.target.closest('.tab-item');
  if (!item) return;
  setView(item.dataset.view);
});
document.getElementById('logoutBtn').addEventListener('click', async () => {
  try { await Api.post('/api/auth/logout'); } catch {}
  window.location.href = '/login';
});

const sidebarEl = document.getElementById('sidebar');
const sidebarBackdrop = document.getElementById('sidebarBackdrop');
function openDrawer()  { sidebarEl.classList.add('open');  sidebarBackdrop.classList.add('open'); }
function closeDrawer() { sidebarEl.classList.remove('open'); sidebarBackdrop.classList.remove('open'); }
document.getElementById('menuBtn').addEventListener('click', openDrawer);
sidebarBackdrop.addEventListener('click', closeDrawer);

const VIEW_META = {
  dashboard: ['Dashboard', 'Platform overview'],
  users:     ['Users', 'Manage accounts and balances'],
  deposits:  ['Deposits', 'Every payment attempt across every user'],
  clients:   ['All Squads', 'Every squad across every user'],
  codes:     ['Redeem Codes', 'Every code generated across every user'],
  settings:  ['Settings', 'Exchange rate, limits, gateway credentials'],
  audit:     ['Activity Log', 'Admin actions, newest first'],
};

function setView(view) {
  State.view = view;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));
  document.querySelectorAll('.tab-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));
  const meta = VIEW_META[view] || ['', ''];
  viewTitle.textContent = meta[0];
  viewSub.textContent = meta[1];
  render();
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function fmtDate(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }

function render() {
  if (State.view === 'dashboard') return renderDashboardView();
  if (State.view === 'users') return renderUsers();
  if (State.view === 'deposits') return renderDeposits();
  if (State.view === 'clients') return renderAllClients();
  if (State.view === 'codes') return renderAllCodes();
  if (State.view === 'settings') return renderSettingsView();
  if (State.view === 'audit') {
    content.innerHTML = `<div class="skeleton" style="height:300px;border-radius:16px;"></div>`;
    refreshAuditLog().then(renderAuditLog);
    return;
  }
}

/* ---------------- dashboard ---------------- */
function statCard(label, value) {
  return `<div class="card stat-card"><div class="label">${escapeHtml(label)}</div><div class="value">${value}</div></div>`;
}

async function renderDashboardView() {
  content.innerHTML = `<div class="skeleton" style="height:140px;border-radius:16px;"></div>`;
  try {
    const res = await Api.get('/api/admin/dashboard');
    State.dashboardStats = res.stats;
    const s = res.stats;
    content.innerHTML = `
      <div class="grid grid-4">
        ${statCard('Total Users', s.total_users)}
        ${statCard('Active Users (7d)', s.active_users_7d)}
        ${statCard('Total Deposits', s.total_deposits)}
        ${statCard('Total Revenue', '$' + s.total_revenue.toFixed(2))}
      </div>
      <div class="grid grid-4" style="margin-top:16px;">
        ${statCard('Pending Payments', s.pending_payments)}
        ${statCard('Total Squads', s.total_squads)}
        ${statCard('Running Squads', s.running_squads)}
        ${statCard('', '')}
      </div>
    `;
    content.lastElementChild.lastElementChild.style.visibility = 'hidden';
  } catch (e) {
    content.innerHTML = `<div class="empty-state"><div class="title">Could not load dashboard</div><div class="sub">${escapeHtml(e.message)}</div></div>`;
  }
}

/* ---------------- users ---------------- */
function renderUsers() {
  content.innerHTML = `
    <div class="card" style="margin-bottom:16px;">
      <input type="text" id="userSearchInput" placeholder="Search by username, email, or user ID…" value="${escapeHtml(State.usersSearch)}" style="width:100%;padding:10px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface-2);color:var(--text);font-size:13.5px;">
    </div>
    <div id="usersTableWrap"></div>
    <div id="usersPager" style="display:flex;justify-content:center;gap:10px;margin-top:16px;"></div>
  `;
  document.getElementById('userSearchInput').addEventListener('input', debounce((e) => {
    State.usersSearch = e.target.value.trim();
    State.usersPage = 0;
    refreshUsers().then(() => renderUsersTable());
  }, 350));
  renderUsersTable();
}

function renderUsersTable() {
  const wrap = document.getElementById('usersTableWrap');
  const pager = document.getElementById('usersPager');
  if (!wrap) return;
  if (!State.users.length) {
    wrap.innerHTML = `<div class="empty-state"><div class="title">No users found</div></div>`;
    if (pager) pager.innerHTML = '';
    return;
  }
  wrap.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Username</th><th>Email</th><th>Role</th><th>Balance</th><th>Squads</th><th>Joined</th><th>Last login</th><th></th></tr></thead>
    <tbody>
      ${State.users.map(u => `
        <tr>
          <td style="font-weight:650;">${escapeHtml(u.username)}</td>
          <td class="muted">${escapeHtml(u.email)}</td>
          <td><span class="pill">${escapeHtml(u.role)}</span></td>
          <td style="font-weight:700;">$${u.balance.toFixed(2)}</td>
          <td>${u.client_count}</td>
          <td class="muted">${new Date(u.created_at * 1000).toLocaleDateString()}</td>
          <td class="muted">${u.last_login_at ? new Date(u.last_login_at * 1000).toLocaleDateString() : 'never'}</td>
          <td><button class="btn btn-sm" data-adjust="${u.id}" data-username="${escapeHtml(u.username)}">Adjust balance</button></td>
        </tr>
      `).join('')}
    </tbody>
  </table></div>`;

  wrap.querySelectorAll('[data-adjust]').forEach(btn => {
    btn.addEventListener('click', () => openBalanceModal(btn.dataset.adjust, btn.dataset.username));
  });

  const totalPages = Math.max(1, Math.ceil(State.usersTotal / PAGE_SIZE));
  if (pager) {
    pager.innerHTML = `
      <button class="btn btn-sm" id="usersPrevBtn" ${State.usersPage <= 0 ? 'disabled' : ''}>Prev</button>
      <span class="muted" style="align-self:center;font-size:12.5px;">Page ${State.usersPage + 1} of ${totalPages}</span>
      <button class="btn btn-sm" id="usersNextBtn" ${State.usersPage >= totalPages - 1 ? 'disabled' : ''}>Next</button>
    `;
    const prev = document.getElementById('usersPrevBtn');
    const next = document.getElementById('usersNextBtn');
    if (prev) prev.addEventListener('click', async () => { State.usersPage--; await refreshUsers(); renderUsersTable(); });
    if (next) next.addEventListener('click', async () => { State.usersPage++; await refreshUsers(); renderUsersTable(); });
  }
}

/* ---------------- deposits ---------------- */
function renderDeposits() {
  content.innerHTML = `
    <div class="card" style="margin-bottom:16px;display:flex;gap:10px;flex-wrap:wrap;">
      <input type="text" id="depositSearchInput" placeholder="Search by username, deposit ID, or gateway transaction ID…" value="${escapeHtml(State.depositsSearch)}" style="flex:1;min-width:220px;padding:10px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface-2);color:var(--text);font-size:13.5px;">
      <select id="depositStatusFilter" style="padding:10px 14px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--surface-2);color:var(--text);font-size:13.5px;">
        <option value="all">All statuses</option>
        <option value="pending">Pending</option>
        <option value="verifying">Verifying</option>
        <option value="success">Successful</option>
        <option value="failed">Failed</option>
      </select>
    </div>
    <div id="depositsTableWrap"></div>
    <div id="depositsPager" style="display:flex;justify-content:center;gap:10px;margin-top:16px;"></div>
  `;
  document.getElementById('depositStatusFilter').value = State.depositsFilter;
  document.getElementById('depositSearchInput').addEventListener('input', debounce((e) => {
    State.depositsSearch = e.target.value.trim();
    State.depositsPage = 0;
    refreshDeposits().then(() => renderDepositsTable());
  }, 350));
  document.getElementById('depositStatusFilter').addEventListener('change', (e) => {
    State.depositsFilter = e.target.value;
    State.depositsPage = 0;
    refreshDeposits().then(() => renderDepositsTable());
  });
  renderDepositsTable();
}

function depositStatusBadge(status) {
  const map = { success: 'running', pending: '', verifying: '', failed: 'stopped' };
  const cls = map[status] ?? '';
  return `<span class="badge ${cls}">${status.toUpperCase()}</span>`;
}

function renderDepositsTable() {
  const wrap = document.getElementById('depositsTableWrap');
  const pager = document.getElementById('depositsPager');
  if (!wrap) return;
  if (!State.deposits.length) {
    wrap.innerHTML = `<div class="empty-state"><div class="title">No deposits found</div></div>`;
    if (pager) pager.innerHTML = '';
    return;
  }
  wrap.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>User</th><th>USD</th><th>BDT</th><th>Rate</th><th>Status</th><th>Gateway Txn ID</th><th>Created</th><th></th></tr></thead>
    <tbody>
      ${State.deposits.map(d => `
        <tr>
          <td style="font-weight:650;">${escapeHtml(d.username)}</td>
          <td>$${d.amount_usd.toFixed(2)}</td>
          <td>৳${d.amount_bdt.toFixed(2)}</td>
          <td class="muted">${d.exchange_rate}</td>
          <td>${depositStatusBadge(d.status)}</td>
          <td class="muted" style="font-family:'SF Mono',Consolas,monospace;font-size:11.5px;">${escapeHtml(d.gateway_transaction_id || '—')}</td>
          <td class="muted">${fmtDate(d.created_at)}</td>
          <td>${(d.status === 'pending' || d.status === 'verifying') ? `<button class="btn btn-sm" data-reverify="${d.id}">Re-verify</button>` : ''}</td>
        </tr>
      `).join('')}
    </tbody>
  </table></div>`;

  wrap.querySelectorAll('[data-reverify]').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = 'Checking…';
      try {
        const res = await Api.post(`/api/admin/deposits/${btn.dataset.reverify}/reverify`);
        Toast.success(`Deposit is now ${res.deposit.status}`);
        await refreshDeposits();
        renderDepositsTable();
      } catch (e) {
        Toast.error(e.message);
        btn.disabled = false;
        btn.textContent = 'Re-verify';
      }
    });
  });

  const totalPages = Math.max(1, Math.ceil(State.depositsTotal / PAGE_SIZE));
  if (pager) {
    pager.innerHTML = `
      <button class="btn btn-sm" id="depositsPrevBtn" ${State.depositsPage <= 0 ? 'disabled' : ''}>Prev</button>
      <span class="muted" style="align-self:center;font-size:12.5px;">Page ${State.depositsPage + 1} of ${totalPages}</span>
      <button class="btn btn-sm" id="depositsNextBtn" ${State.depositsPage >= totalPages - 1 ? 'disabled' : ''}>Next</button>
    `;
    const prev = document.getElementById('depositsPrevBtn');
    const next = document.getElementById('depositsNextBtn');
    if (prev) prev.addEventListener('click', async () => { State.depositsPage--; await refreshDeposits(); renderDepositsTable(); });
    if (next) next.addEventListener('click', async () => { State.depositsPage++; await refreshDeposits(); renderDepositsTable(); });
  }
}

/* ---------------- all clients (squads) ---------------- */
function renderAllClients() {
  if (!State.clients.length) {
    content.innerHTML = `<div class="empty-state"><div class="title">No squads yet</div></div>`;
    return;
  }
  content.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Name</th><th>Owner</th><th>Mode</th><th>Status</th><th>Online</th><th></th></tr></thead>
    <tbody>
      ${State.clients.map(c => `
        <tr>
          <td style="font-weight:650;">${escapeHtml(c.name)}</td>
          <td class="muted">${escapeHtml(c.owner_username)}</td>
          <td>${c.mode === 'with_me' ? 'Play with me' : 'Normal'}</td>
          <td><span class="badge ${c.live_running ? 'running' : 'stopped'}">${c.live_running ? 'RUNNING' : 'STOPPED'}</span></td>
          <td>${c.online_count || 0}/${c.bot_count || 4}</td>
          <td style="display:flex;gap:6px;">
            <button class="btn btn-sm" data-stop="${c.id}">Stop</button>
            <button class="btn btn-sm btn-danger" data-delete="${c.id}" data-name="${escapeHtml(c.name)}">Delete</button>
          </td>
        </tr>
      `).join('')}
    </tbody>
  </table></div>`;

  content.querySelectorAll('[data-stop]').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await Api.post(`/api/admin/clients/${btn.dataset.stop}/stop`);
        Toast.success('Squad stopped');
        await refreshAllClients();
        renderAllClients();
      } catch (e) { Toast.error(e.message); btn.disabled = false; }
    });
  });
  content.querySelectorAll('[data-delete]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(`Delete "${btn.dataset.name}"? This stops and removes it permanently.`)) return;
      try {
        await Api.del(`/api/admin/clients/${btn.dataset.delete}`);
        Toast.success('Squad deleted');
        await refreshAllClients();
        renderAllClients();
      } catch (e) { Toast.error(e.message); }
    });
  });
}

/* ---------------- all redeem codes ---------------- */
function renderAllCodes() {
  if (!State.codes.length) {
    content.innerHTML = `<div class="empty-state"><div class="title">No redeem codes yet</div></div>`;
    return;
  }
  content.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Code</th><th>Value</th><th>Created by</th><th>Status</th><th>Redeemed by</th></tr></thead>
    <tbody>
      ${State.codes.map(c => `
        <tr>
          <td style="font-family:'SF Mono',Consolas,monospace;font-size:12px;">${escapeHtml(c.code)}</td>
          <td style="font-weight:700;">$${c.value.toFixed(2)}</td>
          <td class="muted">${escapeHtml(c.created_by_username)}</td>
          <td>${c.redeemed_by ? '<span class="badge running">REDEEMED</span>' : '<span class="muted">Pending</span>'}</td>
          <td class="muted">${c.redeemed_by_username ? escapeHtml(c.redeemed_by_username) : '—'}</td>
        </tr>
      `).join('')}
    </tbody>
  </table></div>`;
}

/* ---------------- settings ---------------- */
function renderSettingsView() {
  content.innerHTML = `<div class="skeleton" style="height:300px;border-radius:16px;"></div>`;
  refreshSettings().then(() => {
    const s = State.settings;
    content.innerHTML = `
      <div class="grid grid-2" style="align-items:start;">
        <div class="card">
          <div style="font-weight:700;font-size:14.5px;margin-bottom:14px;">Exchange rate &amp; deposit limits</div>
          <form id="ratesForm">
            <div class="form-row">
              <label for="s_rate">1 USD = ? BDT</label>
              <input type="number" id="s_rate" min="0.01" step="0.01" value="${s.usd_to_bdt_rate}" required>
            </div>
            <div class="form-row">
              <label for="s_min">Minimum deposit (USD)</label>
              <input type="number" id="s_min" min="0.01" step="0.01" value="${s.min_deposit_usd}" required>
            </div>
            <div class="form-row">
              <label for="s_max">Maximum deposit (USD)</label>
              <input type="number" id="s_max" min="0.01" step="0.01" value="${s.max_deposit_usd}" required>
            </div>
            <button type="submit" class="btn btn-accent btn-block">Save rate &amp; limits</button>
          </form>
        </div>

        <div class="card">
          <div style="font-weight:700;font-size:14.5px;margin-bottom:4px;">ZR Pay BD gateway</div>
          <div class="muted" style="font-size:12.5px;margin-bottom:14px;">
            Status: ${s.zrpay_configured ? '<span class="badge running">CONFIGURED</span>' : '<span class="badge stopped">NOT CONFIGURED</span>'}
            ${s.zrpay_api_key_set ? `<span class="muted" style="margin-left:8px;">Current key: ${escapeHtml(s.zrpay_api_key_masked)}</span>` : ''}
          </div>
          <form id="gatewayForm">
            <div class="form-row">
              <label for="s_api_key">API Key</label>
              <input type="text" id="s_api_key" placeholder="${s.zrpay_api_key_set ? 'Leave blank to keep current' : 'API-KEY'}" autocomplete="off">
            </div>
            <div class="form-row">
              <label for="s_secret_key">Secret Key</label>
              <input type="password" id="s_secret_key" placeholder="${s.zrpay_secret_key_set ? 'Leave blank to keep current' : 'SECRET-KEY'}" autocomplete="off">
            </div>
            <div class="form-row">
              <label for="s_brand_key">Brand Key</label>
              <input type="text" id="s_brand_key" placeholder="${s.zrpay_brand_key_set ? 'Leave blank to keep current' : 'BRAND-KEY'}" autocomplete="off">
            </div>
            <button type="submit" class="btn btn-accent btn-block">Save gateway credentials</button>
          </form>
        </div>
      </div>

      <div class="grid grid-2" style="align-items:start;margin-top:16px;">
        <div class="card">
          <div style="font-weight:700;font-size:14.5px;margin-bottom:14px;">Maintenance mode</div>
          <form id="maintenanceForm">
            <div class="form-row">
              <label style="display:flex;align-items:center;gap:8px;">
                <input type="checkbox" id="s_maintenance" ${s.maintenance_mode ? 'checked' : ''} style="width:auto;">
                Enable maintenance mode (blocks non-admin API access)
              </label>
            </div>
            <div class="form-row">
              <label for="s_maintenance_msg">Maintenance message</label>
              <textarea id="s_maintenance_msg" rows="2">${escapeHtml(s.maintenance_message)}</textarea>
            </div>
            <button type="submit" class="btn btn-accent btn-block">Save maintenance settings</button>
          </form>
        </div>

        <div class="card">
          <div style="font-weight:700;font-size:14.5px;margin-bottom:14px;">Site announcement</div>
          <form id="announcementForm">
            <div class="form-row">
              <label for="s_announcement">Banner text <span class="muted">(blank = hidden)</span></label>
              <textarea id="s_announcement" rows="2" placeholder="e.g. Scheduled maintenance tonight 2–3 AM">${escapeHtml(s.announcement)}</textarea>
            </div>
            <div class="form-row">
              <label for="s_site_name">Site name</label>
              <input type="text" id="s_site_name" value="${escapeHtml(s.site_name)}">
            </div>
            <button type="submit" class="btn btn-accent btn-block">Save announcement</button>
          </form>
        </div>
      </div>
    `;

    bindSettingsForm('ratesForm', () => ({
      usd_to_bdt_rate: parseFloat(document.getElementById('s_rate').value),
      min_deposit_usd: parseFloat(document.getElementById('s_min').value),
      max_deposit_usd: parseFloat(document.getElementById('s_max').value),
    }));
    bindSettingsForm('gatewayForm', () => {
      const body = {};
      const api = document.getElementById('s_api_key').value.trim();
      const secret = document.getElementById('s_secret_key').value.trim();
      const brand = document.getElementById('s_brand_key').value.trim();
      if (api) body.zrpay_api_key = api;
      if (secret) body.zrpay_secret_key = secret;
      if (brand) body.zrpay_brand_key = brand;
      return body;
    });
    bindSettingsForm('maintenanceForm', () => ({
      maintenance_mode: document.getElementById('s_maintenance').checked,
      maintenance_message: document.getElementById('s_maintenance_msg').value.trim(),
    }));
    bindSettingsForm('announcementForm', () => ({
      announcement: document.getElementById('s_announcement').value.trim(),
      site_name: document.getElementById('s_site_name').value.trim(),
    }));
  });
}

function bindSettingsForm(formId, collectFn) {
  const form = document.getElementById(formId);
  if (!form) return;
  let inFlight = false;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (inFlight) return;
    inFlight = true;
    const btn = form.querySelector('button[type="submit"]');
    const original = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Saving…';
    try {
      const body = collectFn();
      if (Object.keys(body).length === 0) {
        Toast.info('Nothing to update — fields left blank keep their current value');
      } else {
        await Api.post('/api/admin/settings', body);
        Toast.success('Settings saved');
        renderSettingsView();
      }
    } catch (err) {
      Toast.error(err.message || 'Could not save settings');
    } finally {
      inFlight = false;
      btn.disabled = false;
      btn.textContent = original;
    }
  });
}

/* ---------------- audit log ---------------- */
function renderAuditLog() {
  content.innerHTML = `<div id="auditTableWrap"></div><div id="auditPager" style="display:flex;justify-content:center;gap:10px;margin-top:16px;"></div>`;
  renderAuditTable();
}

function renderAuditTable() {
  const wrap = document.getElementById('auditTableWrap');
  const pager = document.getElementById('auditPager');
  if (!wrap) return;
  if (!State.auditEntries.length) {
    wrap.innerHTML = `<div class="empty-state"><div class="title">No activity logged yet</div></div>`;
    if (pager) pager.innerHTML = '';
    return;
  }
  wrap.innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>Admin</th><th>Action</th><th>Target</th><th>Detail</th><th>When</th></tr></thead>
    <tbody>
      ${State.auditEntries.map(a => `
        <tr>
          <td style="font-weight:650;">${escapeHtml(a.admin_username)}</td>
          <td><span class="pill">${escapeHtml(a.action)}</span></td>
          <td class="muted">${a.target_type ? `${escapeHtml(a.target_type)}:${escapeHtml((a.target_id || '').slice(0, 10))}` : '—'}</td>
          <td class="muted" style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(a.detail || '—')}</td>
          <td class="muted">${fmtDate(a.created_at)}</td>
        </tr>
      `).join('')}
    </tbody>
  </table></div>`;

  const totalPages = Math.max(1, Math.ceil(State.auditTotal / PAGE_SIZE));
  if (pager) {
    pager.innerHTML = `
      <button class="btn btn-sm" id="auditPrevBtn" ${State.auditPage <= 0 ? 'disabled' : ''}>Prev</button>
      <span class="muted" style="align-self:center;font-size:12.5px;">Page ${State.auditPage + 1} of ${totalPages}</span>
      <button class="btn btn-sm" id="auditNextBtn" ${State.auditPage >= totalPages - 1 ? 'disabled' : ''}>Next</button>
    `;
    const prev = document.getElementById('auditPrevBtn');
    const next = document.getElementById('auditNextBtn');
    if (prev) prev.addEventListener('click', async () => { State.auditPage--; await refreshAuditLog(); renderAuditTable(); });
    if (next) next.addEventListener('click', async () => { State.auditPage++; await refreshAuditLog(); renderAuditTable(); });
  }
}

/* ---------------- balance modal ---------------- */
const balanceBackdrop = document.getElementById('balanceModalBackdrop');
const balanceForm = document.getElementById('balanceForm');
let balanceAction = 'add';
let balanceSubmitInFlight = false;

function openBalanceModal(userId, username) {
  State.targetUserId = userId;
  State.targetUsername = username;
  document.getElementById('balanceModalTitle').textContent = `Adjust Balance — ${username}`;
  balanceForm.reset();
  balanceAction = 'add';
  document.querySelectorAll('#balanceForm .mode-opt').forEach(o => o.classList.toggle('selected', o.dataset.action === 'add'));
  balanceBackdrop.classList.add('open');
}
function closeBalanceModal() { balanceBackdrop.classList.remove('open'); }
document.getElementById('closeBalanceModal').addEventListener('click', closeBalanceModal);
document.getElementById('cancelBalanceBtn').addEventListener('click', closeBalanceModal);
balanceBackdrop.addEventListener('click', (e) => { if (e.target === balanceBackdrop) closeBalanceModal(); });

document.querySelectorAll('#balanceForm .mode-opt').forEach(opt => {
  opt.addEventListener('click', () => {
    balanceAction = opt.dataset.action;
    document.querySelectorAll('#balanceForm .mode-opt').forEach(o => o.classList.toggle('selected', o === opt));
  });
});

balanceForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (balanceSubmitInFlight) return;
  const btn = document.getElementById('submitBalanceBtn');
  const amount = parseFloat(document.getElementById('f_balance_amount').value);
  if (!amount || amount <= 0) { Toast.error('Enter a valid amount'); return; }
  const delta = balanceAction === 'add' ? amount : -amount;
  balanceSubmitInFlight = true;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Applying…';
  try {
    await Api.post(`/api/admin/users/${State.targetUserId}/balance/adjust`, { delta });
    Toast.success(`Balance ${balanceAction === 'add' ? 'added' : 'removed'} for ${State.targetUsername}`);
    closeBalanceModal();
    await refreshUsers();
    if (State.view === 'users') renderUsersTable();
  } catch (err) {
    Toast.error(err.message || 'Could not adjust balance');
  } finally {
    balanceSubmitInFlight = false;
    btn.disabled = false;
    btn.textContent = 'Apply';
  }
});

/* ---------------- utils ---------------- */
function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/* ---------------- data fetch ---------------- */
async function refreshUsers() {
  try {
    const res = await Api.get(`/api/admin/users?limit=${PAGE_SIZE}&offset=${State.usersPage * PAGE_SIZE}${State.usersSearch ? '&search=' + encodeURIComponent(State.usersSearch) : ''}`);
    State.users = res.users || [];
    State.usersTotal = res.total || 0;
  } catch (e) { console.warn('users refresh failed', e.message); }
}
async function refreshDeposits() {
  try {
    const qs = new URLSearchParams({ limit: PAGE_SIZE, offset: State.depositsPage * PAGE_SIZE });
    if (State.depositsFilter !== 'all') qs.set('status', State.depositsFilter);
    if (State.depositsSearch) qs.set('search', State.depositsSearch);
    const res = await Api.get(`/api/admin/deposits?${qs.toString()}`);
    State.deposits = res.deposits || [];
    State.depositsTotal = res.total || 0;
  } catch (e) { console.warn('deposits refresh failed', e.message); }
}
async function refreshAllClients() {
  try { const res = await Api.get('/api/admin/clients'); State.clients = res.clients || []; }
  catch (e) { console.warn('clients refresh failed', e.message); }
}
async function refreshAllCodes() {
  try { const res = await Api.get('/api/admin/redeem-codes'); State.codes = res.codes || []; }
  catch (e) { console.warn('codes refresh failed', e.message); }
}
async function refreshSettings() {
  try { const res = await Api.get('/api/admin/settings'); State.settings = res.settings; }
  catch (e) { console.warn('settings refresh failed', e.message); }
}
async function refreshAuditLog() {
  try {
    const res = await Api.get(`/api/admin/audit-log?limit=${PAGE_SIZE}&offset=${State.auditPage * PAGE_SIZE}`);
    State.auditEntries = res.entries || [];
    State.auditTotal = res.total || 0;
  } catch (e) { console.warn('audit log refresh failed', e.message); }
}

(async function boot() {
  await Promise.all([refreshUsers(), refreshAllClients(), refreshAllCodes(), refreshDeposits()]);
  render();
  setInterval(async () => {
    if (State.view === 'dashboard') renderDashboardView();
    else if (State.view === 'users') { await refreshUsers(); renderUsersTable(); }
    else if (State.view === 'deposits') { await refreshDeposits(); renderDepositsTable(); }
    else if (State.view === 'clients') { await refreshAllClients(); renderAllClients(); }
    else if (State.view === 'codes') { await refreshAllCodes(); renderAllCodes(); }
    else if (State.view === 'audit') { await refreshAuditLog(); renderAuditTable(); }
  }, 12000);
})();
