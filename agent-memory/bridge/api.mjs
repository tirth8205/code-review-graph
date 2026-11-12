const CODES = new Set(['invalid_input', 'unauthorized', 'session_not_found', 'conflicting_turn', 'revision_conflict', 'openai_not_configured', 'upstream_unavailable']);
export class BridgeError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
export function required(value, field, max = 100000) {
  if (typeof value !== 'string' || !value.trim() || value.length > max || value.includes('\u0000')) throw new BridgeError('invalid_input', `${field} must be a nonempty string of at most ${max} characters without NUL`);
  return value;
}
export function configuration(env = process.env) {
  let url;
  try { url = new URL(required(env.MEMORY_LENS_API_URL, 'MEMORY_LENS_API_URL', 2048)); } catch { throw new BridgeError('configuration', 'Set MEMORY_LENS_API_URL to the memory-api endpoint'); }
  const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.username || url.password || url.hash || !(url.protocol === 'https:' || (url.protocol === 'http:' && loopback))) throw new BridgeError('configuration', 'API endpoint requires HTTPS (HTTP is allowed only for localhost development), without embedded credentials');
  const timeout = Number(env.MEMORY_LENS_TIMEOUT_MS ?? 30000);
  if (!Number.isInteger(timeout) || timeout < 50 || timeout > 120000) throw new BridgeError('configuration', 'MEMORY_LENS_TIMEOUT_MS must be 50–120000');
  if (env.MEMORY_LENS_LAYERED_CONTEXT !== undefined && !['0', '1'].includes(env.MEMORY_LENS_LAYERED_CONTEXT)) throw new BridgeError('configuration', 'MEMORY_LENS_LAYERED_CONTEXT must be 0 or 1');
  return { url: url.href, token: env.MEMORY_LENS_TOKEN || '', timeout, env };
}
export async function api(config, body, { unauthenticated = false, timeout = config.timeout } = {}) {
  if (!unauthenticated) required(config.token, 'MEMORY_LENS_TOKEN', 4096);
  let response;
  try {
    response = await fetch(config.url, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(unauthenticated ? {} : { Authorization: `Bearer ${config.token}` }) }, body: JSON.stringify(body), redirect: 'error', signal: AbortSignal.timeout(timeout) });
  } catch { throw new BridgeError('upstream_unavailable', 'Memory API unavailable or timed out'); }
  let data;
  try {
    const raw = await response.text();
    if (raw.length > 2000000) throw new Error();
    data = JSON.parse(raw);
  } catch { throw new BridgeError('upstream_unavailable', 'Memory API returned an invalid response'); }
  if (!response.ok || data?.error) {
    const code = CODES.has(data?.error?.code) ? data.error.code : 'upstream_unavailable';
    throw new BridgeError(code, `Memory API request failed (${code})`);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new BridgeError('upstream_unavailable', 'Memory API returned an invalid response');
  if (body.session_id && data.session_id !== body.session_id) throw new BridgeError('upstream_unavailable', 'Memory API returned a different session');
  return data;
}
export function safeError(error) {
  return { code: error instanceof BridgeError ? error.code : 'local_error', message: error instanceof BridgeError ? error.message : 'Memory Lens local operation failed' };
}
