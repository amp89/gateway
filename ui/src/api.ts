// Admin API client. The admin prefix is the first path segment, so random prefixes work unchanged.
const seg = location.pathname.split('/').filter(Boolean)[0] ?? 'admin';
export const base = `/${seg}/api`;

let csrf = '';
export function setCsrf(token: string | undefined) {
  csrf = token ?? '';
}

// Plain-language text for error codes; the server's detail (if any) is appended.
const MESSAGES: Record<string, string> = {
  bad_request: 'Not accepted',
  name_taken: 'That name is already used',
  not_found: 'Not found (maybe deleted meanwhile; reload the page)',
  unknown_user: 'Add these IDs under Bots → Telegram users first',
  csrf: 'Session check failed; reload the page',
  unauthorized: 'Logged out; log in again',
  forbidden: 'Not allowed',
  revoked: 'That key is revoked',
  wrong_password: 'Wrong password',
  wrong_passphrase: 'Wrong passphrase',
  locked: 'Too many attempts',
  already_initialized: 'Already set up',
  wrong_setup_code: 'Wrong setup code (see the start script output)',
  upstream_error: 'The upstream API refused',
  upstream_timeout: 'The upstream API did not answer in time',
  sealed: 'The gateway is sealed; unlock it first',
  scope_in_use: 'Keys are limited by a scope field you removed; remove those limits on Permissions first',
  busy: 'Another attempt is running; try again in a moment',
};

function describe(status: number, body: any): string {
  const code = body?.error;
  const base = (code && MESSAGES[code]) || code || `HTTP ${status}`;
  const extra: string[] = [];
  if (body?.detail) extra.push(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
  if (body?.user_ids) extra.push(`IDs: ${body.user_ids.join(', ')}`);
  if (body?.retry_after) extra.push(`retry in ${body.retry_after}s`);
  return extra.length ? `${base}: ${extra.join('; ')}` : base;
}

export class ApiError extends Error {
  constructor(public status: number, public body: any) {
    super(describe(status, body));
  }
}

const listeners: Array<() => void> = [];
export function onUnauthorized(fn: () => void) {
  listeners.push(fn);
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrf && method !== 'GET') headers['X-CSRF-Token'] = csrf;
  const r = await fetch(base + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
  });
  let data: any = null;
  try {
    data = await r.json();
  } catch {
    data = null;
  }
  if (!r.ok) {
    if (r.status === 401 || (r.status === 503 && data?.error === 'sealed')) listeners.forEach((f) => f());
    throw new ApiError(r.status, data);
  }
  return data as T;
}

export const get = <T = any>(p: string) => api<T>('GET', p);
export const post = <T = any>(p: string, b?: unknown) => api<T>('POST', p, b ?? {});
export const put = <T = any>(p: string, b?: unknown) => api<T>('PUT', p, b ?? {});
export const patch = <T = any>(p: string, b?: unknown) => api<T>('PATCH', p, b ?? {});
export const del = <T = any>(p: string) => api<T>('DELETE', p);

/** Copy to the clipboard; falls back to execCommand where the Clipboard API is missing (plain-HTTP origins). */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = document.createElement('textarea');
  ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
  // inside an open modal the rest of the page is inert, so put the textarea in the top dialog
  const host = [...document.querySelectorAll('dialog[open]')].pop() ?? document.body;
  host.appendChild(ta); ta.select();
  const ok = document.execCommand('copy');
  ta.remove();
  if (!ok) throw new Error('Copy failed: select the text and copy it by hand');
}

export function when(ts: number | null | undefined): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}
