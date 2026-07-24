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

/** Snapshot age from the last response, so the header can show data freshness. */
export const freshness = { ageSeconds: null };

function buildUrl(path, params = {}) {
  const url = new URL(BASE + path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    // Distinguish "not set" from a meaningful false/0.
    if (value === undefined || value === null || value === '') continue;
    url.searchParams.set(key, String(value));
  }
  return url;
}

async function request(path, params, { method = 'GET' } = {}) {
  let response;
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      headers: { accept: 'application/json' },
    });
  } catch {
    throw new ApiError('Network request failed', { code: 'network_error', status: 0 });
  }

  const age = response.headers.get('x-snapshot-age-seconds');
  if (age !== null) freshness.ageSeconds = Number(age);

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
  hierarchy: (depth = 7) => request('/hierarchy', { depth }),

  // POST through the same path as every other call, so a throttled (429) or upstream
  // failure surfaces the server's error `code`/`message`/`request_id` and an actionable
  // hint, instead of a generic "Refresh failed".
  refresh: () => request('/system/snapshot/refresh', undefined, { method: 'POST' }),
};
