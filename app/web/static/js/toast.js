// toast.js — animated toasts with progress bar, stacking, max limit
const Toast = (() => {
  const MAX_TOASTS = 4;
  const DEFAULT_TTL = 3800;

  const ICONS = {
    success: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>',
    error:   '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>',
    warning: '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9L1.8 18a1.5 1.5 0 001.3 2.3h17.8a1.5 1.5 0 001.3-2.3L13.7 3.9a1.5 1.5 0 00-2.6 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
    info:    '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 16v-5"/><path d="M12 8h.01"/></svg>',
  };

  function wrap() {
    let el = document.getElementById('toastWrap');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toastWrap';
      el.className = 'toast-wrap';
      document.body.appendChild(el);
    }
    return el;
  }

  function dismiss(el) {
    if (!el || el.dataset.dismissing) return;
    el.dataset.dismissing = '1';
    el.style.transition = 'opacity .24s var(--ease), transform .24s var(--ease)';
    el.style.opacity = '0';
    el.style.transform = 'translateX(24px)';
    setTimeout(() => el.remove(), 260);
  }

  function show(message, type = 'info', ttl = DEFAULT_TTL) {
    const container = wrap();

    // Cap active toasts
    const existing = container.querySelectorAll('.toast:not([data-dismissing])');
    if (existing.length >= MAX_TOASTS) {
      dismiss(existing[0]);
    }

    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.style.setProperty('--toast-ttl', ttl + 'ms');
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');

    el.innerHTML = `
      <span class="icon-chip">${ICONS[type] || ICONS.info}</span>
      <span class="toast-msg">${escapeHtml(message)}</span>
      <button class="toast-close" aria-label="Dismiss">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
      </button>
    `;

    el.querySelector('.toast-close').addEventListener('click', () => dismiss(el));
    container.appendChild(el);

    const timer = setTimeout(() => dismiss(el), ttl);
    el.addEventListener('mouseenter', () => clearTimeout(timer), { once: true });

    return el;
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  return {
    success: (m, ttl) => show(m, 'success', ttl),
    error:   (m, ttl) => show(m, 'error',   ttl ?? 5000),
    warning: (m, ttl) => show(m, 'warning', ttl),
    info:    (m, ttl) => show(m, 'info',    ttl),
  };
})();