import { createServer } from 'node:http';
import { createHash, timingSafeEqual } from 'node:crypto';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { ApiError, invalid } from './errors.mjs';
import { MemoryStore } from './store.mjs';
import { AstraProvider } from './provider.mjs';
import { MemoryService } from './service.mjs';

const digest = value => createHash('sha256').update(value).digest();
const localhostOrigin = value => {
  try { const url = new URL(value); return url.origin === value && ['http:', 'https:'].includes(url.protocol) && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname); }
  catch { return false; }
};

export async function startMemoryServer(options = {}) {
  const token = options.token ?? process.env.MEMORY_LENS_TOKEN;
  if (typeof token !== 'string' || token.length < 16) throw new Error('MEMORY_LENS_TOKEN must be configured with at least 16 characters before starting the API.');
  const expected = digest(token);
  const apiKey = options.apiKey ?? process.env.OPENAI_API_KEY ?? '';
  const allowed = options.allowedOrigins ?? (process.env.MEMORY_LENS_ALLOWED_ORIGINS || '').split(',').map(value => value.trim()).filter(Boolean);
  if (allowed.some(origin => origin === '*' || new URL(origin).origin !== origin)) throw new Error('MEMORY_LENS_ALLOWED_ORIGINS must contain exact origins, never a wildcard.');
  let codeGraph = options.codeGraph ?? null;
  if (options.codeGraph === undefined && (process.env.MEMORY_LENS_CODE_GRAPH_REPO || process.env.MEMORY_LENS_CODE_GRAPH_COMMAND_JSON)) {
    const { ExistingCodeGraph } = await import('../bridge/existing-graph.mjs');
    codeGraph = new ExistingCodeGraph();
  }
  const store = new MemoryStore(options.dbPath ?? process.env.MEMORY_LENS_DB ?? join(homedir(), '.memory-lens', 'memory.sqlite'));
  const provider = new AstraProvider({ apiKey, ...(options.providerUrl ? { providerUrl: options.providerUrl } : {}), ...(options.fetchImpl ? { fetchImpl: options.fetchImpl } : {}), ...(options.modelTimeoutMs ? { modelTimeoutMs: options.modelTimeoutMs } : {}) });
  const service = new MemoryService(store, provider, { codeGraph });
  const server = createServer(async (req, res) => {
    const respond = (status, body) => { if (!res.destroyed) { res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' }); res.end(JSON.stringify(body)); } };
    try {
      const origin = req.headers.origin;
      if (origin && !(allowed.includes(origin) || (allowed.length === 0 && localhostOrigin(origin)))) throw new ApiError(403, 'origin_not_allowed', 'This browser origin is not allowed by the local API.');
      if (origin) { res.setHeader('access-control-allow-origin', origin); res.setHeader('vary', 'Origin'); res.setHeader('access-control-allow-methods', 'POST, OPTIONS'); res.setHeader('access-control-allow-headers', 'Authorization, Content-Type'); }
      if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }
      if (req.method !== 'POST') throw new ApiError(405, 'invalid_input', 'Use POST with a JSON action.');
      if (!(req.headers['content-type'] || '').startsWith('application/json')) throw invalid('Content-Type must be application/json.');
      let bytes = 0;
      const chunks = [];
      for await (const chunk of req) { bytes += chunk.length; if (bytes > 100_000) throw invalid('Request body exceeds 100,000 bytes.'); chunks.push(chunk); }
      const raw = Buffer.concat(chunks).toString('utf8');
      let body;
      try { body = JSON.parse(raw); } catch { throw invalid('Request body must be valid JSON.'); }
      if (body?.action !== 'health') {
        const auth = req.headers.authorization || '';
        if (!auth.startsWith('Bearer ') || !timingSafeEqual(digest(auth.slice(7)), expected)) throw new ApiError(401, 'unauthorized', 'A valid memory API bearer credential is required.');
      }
      respond(200, await service.action(body));
    } catch (error) {
      const safe = error instanceof ApiError ? error : new ApiError(500, 'internal_error', 'The memory API could not complete this request.');
      respond(safe.status, { error: { code: safe.code, message: safe.message } });
    }
  });
  server.requestTimeout = 15_000;
  server.headersTimeout = 10_000;
  server.keepAliveTimeout = 1000;
  const host = options.host ?? process.env.MEMORY_LENS_HOST ?? '127.0.0.1';
  const port = Number(options.port ?? process.env.MEMORY_LENS_PORT ?? 0);
  try { await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, host, resolve); }); }
  catch (error) { try { await codeGraph?.close(); } finally { store.close(); } throw error; }
  const address = server.address();
  let closing;
  return { url: `http://${host.includes(':') ? '[' + host + ']' : host}:${address.port}/memory-api`, close: () => closing ??= (async () => { await new Promise(resolve => server.close(resolve)); try { await codeGraph?.close(); } finally { store.close(); } })() };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const app = await startMemoryServer({ port: Number(process.env.MEMORY_LENS_PORT || 8787) });
    process.stdout.write(`Memory Lens local API: ${app.url}\nOpenAI extraction: ${process.env.OPENAI_API_KEY ? 'configured; not yet verified' : 'OPENAI_API_KEY required'}\n`);
    let closing = false;
    const stop = async () => { if (!closing) { closing = true; await app.close(); process.exit(0); } };
    process.on('SIGTERM', stop); process.on('SIGINT', stop);
  } catch (error) { process.stderr.write(`Memory API startup failed: ${error.message}\n`); process.exitCode = 1; }
}
