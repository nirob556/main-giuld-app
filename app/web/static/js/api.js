// api.js — fetch wrapper with timeout, structured errors, 401 redirect
const Api = (() => {
  const DEFAULT_TIMEOUT = 20000;

  async function call(method, path, body, opts = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), opts.timeout ?? DEFAULT_TIMEOUT);

    const fetchOpts = {
      method,
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal,
    };
    if (body !== undefined) fetchOpts.body = JSON.stringify(body);

    let res;
    try {
      res = await fetch(path, fetchOpts);
    } catch (err) {
      clearTimeout(timeout);
      if (err.name === 'AbortError') {
        throw new Error('Request timed out. Please try again.');
      }
      throw new Error('Network error — check your connection.');
    }
    clearTimeout(timeout);

    if (res.status === 401) {
      window.location.href = '/login';
      throw new Error('Session expired. Redirecting to login…');
    }

    let data = {};
    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      data = await res.json().catch(() => ({}));
    } else {
      const text = await res.text().catch(() => '');
      data = text ? { detail: text } : {};
    }

    if (!res.ok && data.status !== 'error') {
      throw new Error(data.detail || data.error || `Request failed (HTTP ${res.status})`);
    }

    return data;
  }

  return {
    get: (path, opts) => call('GET', path, undefined, opts),
    post: (path, body, opts) => call('POST', path, body, opts),
    patch: (path, body, opts) => call('PATCH', path, body, opts),
    del: (path, opts) => call('DELETE', path, undefined, opts),
  };
})();