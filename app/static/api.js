/**
 * Typed-ish client for the Urja Meter Ops API.
 *
 * Two responsibilities only: build URLs and turn failures into something the UI can
 * render. Every call resolves to data or throws an `ApiError` carrying the server's own
 * error `code` — the UI never has to parse prose to decide what to show.
 */

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(message, { code = 'unknown', status = 0, requestId = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.requestId = requestId;
  }

  /** Guidance for the operator, not a restatement of the failure. */
  get hint() {
    if (this.code === 'upstream_rate_limited') return 'The portal is throttling us. Retry shortly.';
    if (this.code === 'upstream_unavailable') return 'The legacy portal is unreachable.';
    if (this.code === 'upstream_timeout') return 'The legacy portal did not respond in time.';
    if (this.code === 'upstream_auth_failed') return 'The service could not sign in to the portal.';
    if (this.code === 'snapshot_unavailable') return 'The index is still building. Retry in a moment.';
    if (this.status === 0) return 'Could not reach the API. Is the service running?';
    return null;
  }
}

/** Snapshot provenance from the last response, so the header can show data age. */
export const freshness = { ageSeconds: null, source: null, builtAt: null };

function buildUrl(path, params = {}) {
  const url = new URL(BASE + path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    // Distinguish "not set" from a meaningful false/0.
    if (value === undefined || value === null || value === '') continue;
    url.searchParams.set(key, String(value));
  }
  return url;
}

async function request(path, params) {
  let response;
  try {
    response = await fetch(buildUrl(path, params), { headers: { accept: 'application/json' } });
  } catch (cause) {
    throw new ApiError('Network request failed', { code: 'network_error', status: 0 });
  }

  const age = response.headers.get('x-snapshot-age-seconds');
  if (age !== null) {
    freshness.ageSeconds = Number(age);
    freshness.source = response.headers.get('x-snapshot-source');
    freshness.builtAt = response.headers.get('x-snapshot-built-at');
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    if (response.ok) throw new ApiError('Response was not valid JSON', { status: response.status });
  }

  if (!response.ok) {
    const error = body?.error ?? {};
    throw new ApiError(error.message || `Request failed (${response.status})`, {
      code: error.code || 'http_error',
      status: response.status,
      requestId: error.request_id ?? response.headers.get('x-request-id'),
    });
  }
  return body;
}

export const api = {
  stats: () => request('/stats'),
  snapshot: () => request('/system/snapshot'),
  dataQuality: () => request('/data-quality'),
  meters: (params) => request('/meters', params),
  meter: (id) => request(`/meters/${encodeURIComponent(id)}`),
  consumption: (id, params) => request(`/meters/${encodeURIComponent(id)}/consumption`, params),
  near: (params) => request('/meters/near', params),
  hierarchy: (depth = 7) => request('/hierarchy', { depth }),
  transformers: (params) => request('/transformers', params),

  refresh: async () => {
    const response = await fetch(`${BASE}/system/snapshot/refresh`, { method: 'POST' });
    if (!response.ok) throw new ApiError('Refresh failed', { status: response.status });
    return response.json();
  },
};
