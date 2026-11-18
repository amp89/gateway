// Client code for one gateway key, meant to be copied to another machine and run there:
// Python (standard library only) and sh + curl. One real function per action the key may use.
import { get } from './api';

export type Act =
  | { kind: 'send'; bot?: string; chat?: number }
  | { kind: 'read'; bot?: string }
  | { kind: 'call'; svc: string; op: string; method: string; tpl: string; desc: string;
      path: string[]; query: string[] | null; body: string[] | null };
export type Ctx = { key: string; url: string; secret?: string; acts: Act[] };

/** What the key may do, with real defaults (scoped values first, else the first bot / allowed user). */
export async function actionsFor(keyId: number): Promise<Act[]> {
  const perms = await get('/permissions');
  const grants: { action: string; scope: Record<string, string[]> | null }[] = perms.grants.filter((g: any) => g.key_id === keyId);
  let bots: any[] = [];
  if (grants.some((g) => g.action.startsWith('telegram.'))) {
    try { bots = (await get('/bots')).bots; } catch { bots = []; }
  }
  const services: any[] = grants.some((g) => !g.action.startsWith('telegram.')) ? (await get('/services')).services : [];
  const opsBySvc: Record<string, any[]> = {};
  const out: Act[] = [];
  for (const g of grants) {
    const scope = g.scope ?? {};
    if (g.action === 'telegram.send' || g.action === 'telegram.read') {
      const bot: string | undefined = scope.bots?.[0] ?? bots[0]?.name;
      if (g.action === 'telegram.read') { out.push({ kind: 'read', bot }); continue; }
      const allowed: number[] = bots.find((b) => b.name === bot)?.allowlist ?? [];
      const chat = Number(scope.chat_ids?.[0] ?? allowed[0]) || undefined;
      out.push({ kind: 'send', bot, chat });
      continue;
    }
    const [svcName, opName] = g.action.split('.', 2);
    const svc = services.find((s) => s.name === svcName);
    if (!svc) continue;
    opsBySvc[svcName] ??= (await get(`/services/${svc.id}/operations`)).operations;
    const op = opsBySvc[svcName].find((o) => o.name === opName);
    if (!op) continue;
    out.push({ kind: 'call', svc: svcName, op: opName, method: op.method, tpl: op.path_template, desc: op.description || '',
      path: [...op.path_template.matchAll(/\{([a-z0-9_]+)\}/g)].map((m) => m[1]),
      query: op.query_fields, body: op.method === 'GET' ? [] : op.body_fields });
  }
  return out;
}

/** Admin-entered text (key names, descriptions) placed in comments and docstrings: one line only, so it can
 * never start a new line of code in the generated file. */
const oneLine = (s: string) => s.replace(/[\u0000-\u001f\u007f\u2028\u2029]+/g, ' ');
/** Same, safe inside a Python triple-quoted docstring. */
const pyDoc = (s: string) => oneLine(s).replaceAll('\\', '\\\\').replaceAll('"', '\\"');

const NO_BOT = 'No Telegram bot yet: add one on the admin Bots page, then pass its name.';
const NO_USER = 'No Telegram user allowed on this bot yet: add one on the admin Bots page.';

// ---- Python ---------------------------------------------------------------------------------
const PY_KEYWORDS = new Set(('False None True and as assert async await break class continue def del elif else except finally for ' +
  'from global if import in is lambda nonlocal not or pass raise return try while with yield').split(' '));

/** A Python identifier for `name`, unique within `used`. */
function pyIdent(name: string, used: Set<string>): string {
  let id = name.replace(/[^A-Za-z0-9_]/g, '_');
  if (/^[0-9]/.test(id)) id = `_${id}`;
  if (PY_KEYWORDS.has(id)) id += '_';
  while (used.has(id)) id += '_';
  used.add(id);
  return id;
}
const pyStr = (s: string) => JSON.stringify(s);
const dictOf = (pairs: string[][]) => `{${pairs.map(([n, id]) => `${pyStr(n)}: ${id}`).join(', ')}}`;

function pyFunction(a: Act, names: Set<string>): string[] {
  if (a.kind === 'send') {
    const fn = pyIdent('send', names);
    const args = ['text', a.chat ? `chat_id=${a.chat}` : 'chat_id', a.bot ? `bot=${pyStr(a.bot)}` : 'bot', 'parse_mode=None', 'silent=False'];
    const note = !a.bot ? `\n    ${NO_BOT}` : !a.chat ? `\n    ${NO_USER}` : '';
    return [
      `def ${fn}(${args.join(', ')}):`,
      `    """telegram.send (built into the gateway): a text message from your bot to an allowed chat.${note}"""`,
      '    body = {"bot": bot, "chat_id": int(chat_id), "text": text}',
      '    if parse_mode:',
      '        body["parse_mode"] = parse_mode',
      '    if silent:',
      '        body["silent"] = True',
      '    return request("POST", "/telegram/send", body)',
    ];
  }
  if (a.kind === 'read') {
    const fn = pyIdent('read', names);
    const args = a.bot ? `since=0, limit=50, bot=${pyStr(a.bot)}` : 'bot, since=0, limit=50';
    return [
      `def ${fn}(${args}):`,
      `    """telegram.read (built into the gateway): messages your bot received, oldest first.${a.bot ? '' : `\n    ${NO_BOT}`}`,
      '    Next page: since=<next_since from the previous answer>."""',
      '    return request("GET", "/telegram/messages", params={"bot": bot, "since": since, "limit": limit})',
    ];
  }
  const fn = pyIdent(`${a.svc}_${a.op}`, names);
  const local = new Set<string>(['query', 'body']);
  const pathArgs = a.path.map((p) => [p, pyIdent(p, local)]);
  const q = (a.query ?? []).map((f) => [f, pyIdent(f, local)]);
  const b = (a.body ?? []).map((f) => [f, pyIdent(f, local)]);
  const keywords = [...q, ...b].map(([, id]) => `${id}=None`);
  if (a.query === null) keywords.push('query=None');
  if (a.body === null) keywords.push('body=None');
  const params = [...pathArgs.map(([, id]) => id), ...(keywords.length ? ['*', ...keywords] : [])];
  const parts = [pyStr(a.svc), pyStr(a.op)];
  if (pathArgs.length) parts.push(`path=${dictOf(pathArgs)}`);
  if (q.length) parts.push(`query=given(${dictOf(q)})`);
  else if (a.query === null) parts.push('query=query');
  if (b.length) parts.push(`body=given(${dictOf(b)})`);
  else if (a.body === null) parts.push('body=body');
  const desc = a.desc ? `: ${pyDoc(a.desc)}` : '';
  return [
    `def ${fn}(${params.join(', ')}):`,
    `    """${a.svc}.${a.op}${desc} (${a.method} ${a.tpl} on the service). Only arguments you pass are sent."""`,
    `    return call(${parts.join(', ')})`,
  ];
}

export function python(c: Ctx): string {
  const names = new Set(['request', 'call', 'given', 'me', 'main']);
  const fns = c.acts.map((a) => pyFunction(a, names));
  const fnNames = fns.map((f) => f[0].slice(4, f[0].indexOf('(')));
  const key = c.secret
    ? `KEY = os.environ.get("GATEWAY_KEY", ${pyStr(c.secret)})  # keep this file private: it contains the key`
    : `KEY = os.environ["GATEWAY_KEY"]  # the key's secret, shown once when it was created`;
  const usage = fnNames.length
    ? fnNames.slice(0, 3).map((n) => `    python3 -c 'import gateway_client as g; print(g.${n}(...))'`)
    : ['    (no actions allowed yet: tick some for this key on the admin Permissions page)'];
  return [
    `"""Gateway client for key "${pyDoc(c.key)}". Python 3, standard library only. Copy to any machine and run.`,
    '',
    'Save as gateway_client.py, then:',
    ...(c.secret ? [] : ['    export GATEWAY_KEY=gw_...']),
    '    python3 gateway_client.py    # prints who you are and what you may do',
    ...usage,
    '',
    `Gateway URL: ${c.url} (override with GATEWAY_URL).`,
    'Errors raise GatewayError(status, body); body["error"] is the gateway error code.',
    '"""',
    'import json',
    'import os',
    'import urllib.error',
    'import urllib.parse',
    'import urllib.request',
    '',
    `BASE = os.environ.get("GATEWAY_URL", ${pyStr(c.url)}).rstrip("/")`,
    key,
    '',
    '',
    'class GatewayError(Exception):',
    '    def __init__(self, status, body):',
    '        self.status, self.body = status, body',
    '        super().__init__(f"HTTP {status}: {body}")',
    '',
    '',
    'def request(method, path, body=None, params=None):',
    '    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")',
    '    data = None if body is None else json.dumps(body).encode()',
    '    req = urllib.request.Request(url, data=data, method=method, headers={',
    '        "Authorization": f"Bearer {KEY}", "Content-Type": "application/json", "User-Agent": "gateway-client/1"})',
    '    try:',
    '        with urllib.request.urlopen(req, timeout=30) as r:',
    '            return json.load(r)',
    '    except urllib.error.HTTPError as e:',
    '        raw = e.read()',
    '        try:',
    '            err = json.loads(raw)',
    '        except ValueError:',
    '            err = {"error": raw.decode(errors="replace")}',
    '        raise GatewayError(e.code, err) from None',
    '',
    '',
    'def given(fields):',
    '    """Drop arguments that were not passed (None)."""',
    '    return {k: v for k, v in fields.items() if v is not None}',
    '',
    '',
    'def call(service, operation, path=None, query=None, body=None):',
    '    """Any service operation. Returns {"status": upstream status, "body": upstream JSON or text}."""',
    '    payload = given({"path": path, "query": query or None, "body": body or None})',
    '    return request("POST", f"/call/{service}/{operation}", payload)',
    '',
    '',
    'def me():',
    `    """This key's name and the actions it may use."""`,
    '    return request("GET", "/me")',
    ...fns.flatMap((f) => ['', '', ...f]),
    '',
    '',
    'if __name__ == "__main__":',
    '    print(json.dumps(me(), indent=2))',
  ].join('\n') + '\n';
}

// ---- sh + curl ------------------------------------------------------------------------------
// Shell lines are plain single-quoted TS strings (no TS interpolation); JSON is built with printf + js().

/** A shell function name for `name`, unique within `used`. */
function shIdent(name: string, used: Set<string>): string {
  let id = name.replace(/[^A-Za-z0-9_]/g, '_');
  while (used.has(id)) id += '_';
  used.add(id);
  return id;
}

function shFunction(a: Act, names: Set<string>): string[] {
  if (a.kind === 'send') {
    const fn = shIdent('send', names);
    return [
      `# telegram.send (built into the gateway): ${fn} TEXT [CHAT_ID] [BOT]`,
      `#   defaults: CHAT_ID=${a.chat ?? '(none)'} BOT=${a.bot ?? '(none)'}`,
      ...(!a.bot ? [`#   ${NO_BOT}`] : !a.chat ? [`#   ${NO_USER}`] : []),
      `${fn}() {`,
      `  _chat="$2"; [ -n "$_chat" ] || _chat='${a.chat ?? ''}'`,
      `  _bot="$3"; [ -n "$_bot" ] || _bot='${a.bot ?? ''}'`,
      `  [ -n "$_chat" ] && [ -n "$_bot" ] || { echo "usage: ${fn} TEXT CHAT_ID BOT" >&2; return 2; }`,
      `  gw -X POST "$GATEWAY_URL/telegram/send" -d "$(printf '{"bot": %s, "chat_id": %s, "text": %s}' "$(js "$_bot")" "$_chat" "$(js "$1")")"`,
      '}',
    ];
  }
  if (a.kind === 'read') {
    const fn = shIdent('read', names);
    return [
      `# telegram.read (built into the gateway): ${fn} [SINCE] [BOT]`,
      `#   defaults: SINCE=0 BOT=${a.bot ?? '(none)'}`,
      ...(!a.bot ? [`#   ${NO_BOT}`] : []),
      `${fn}() {`,
      `  _since="$1"; [ -n "$_since" ] || _since=0`,
      `  _bot="$2"; [ -n "$_bot" ] || _bot='${a.bot ?? ''}'`,
      `  [ -n "$_bot" ] || { echo "usage: ${fn} SINCE BOT" >&2; return 2; }`,
      '  gw -G "$GATEWAY_URL/telegram/messages" --data-urlencode "bot=$_bot" --data-urlencode "since=$_since"',
      '}',
    ];
  }
  const fn = shIdent(`${a.svc}_${a.op}`, names);
  const usage = a.path.map((p) => p.toUpperCase());
  const notes: string[] = [];
  const lines: string[] = [];
  const fmt: string[] = [];
  const vals: string[] = [];
  if (a.path.length) {
    lines.push(`  [ "$#" -ge ${a.path.length} ] || { echo "usage: ${fn} ${usage.join(' ')}" >&2; return 2; }`);
    fmt.push(`"path": {${a.path.map((p) => `"${p}": %s`).join(', ')}}`);
    a.path.forEach((_, i) => vals.push(`"$(js "$${i + 1}")"`));
  }
  let n = a.path.length;
  if (a.query === null || a.query.length) {
    n += 1;
    usage.push('[QUERY_JSON]');
    notes.push(`#   QUERY_JSON fields: ${a.query === null ? 'any' : a.query.join(', ')}`);
    lines.push(`  _q="$${n}"; [ -n "$_q" ] || _q='{}'`);
    fmt.push('"query": %s');
    vals.push('"$_q"');
  }
  if (a.body === null || a.body.length) {
    n += 1;
    usage.push('[BODY_JSON]');
    notes.push(`#   BODY_JSON fields: ${a.body === null ? 'any' : a.body.join(', ')}`);
    lines.push(`  _b="$${n}"; [ -n "$_b" ] || _b='{}'`);
    fmt.push('"body": %s');
    vals.push('"$_b"');
  }
  const data = fmt.length ? `"$(printf '{${fmt.join(', ')}}' ${vals.join(' ')})"` : `'{}'`;
  return [
    `# ${a.svc}.${a.op}${a.desc ? `: ${oneLine(a.desc)}` : ''} (${a.method} ${a.tpl} on the service)`,
    `#   ${[fn, ...usage].join(' ')}`,
    ...notes,
    `${fn}() {`,
    ...lines,
    `  gw -X POST "$GATEWAY_URL/call/${a.svc}/${a.op}" -d ${data}`,
    '}',
  ];
}

export function shell(c: Ctx): string {
  const names = new Set(['gw', 'js', 'me']);
  const fns = c.acts.map((a) => shFunction(a, names));
  const url = c.url.replace(/\/+$/, '');
  const key = c.secret
    ? 'GATEWAY_KEY="${GATEWAY_KEY:-' + c.secret + '}"  # keep this file private: it contains the key'
    : ': "${GATEWAY_KEY:?export GATEWAY_KEY=gw_... (the key\'s secret, shown once when it was created)}"';
  return [
    '#!/bin/sh',
    `# Gateway client for key "${oneLine(c.key)}": sh + curl. Copy to any machine as gateway.sh.`,
    '#   sh gateway.sh                  -> who you are and what you may do',
    '#   sh gateway.sh FUNCTION ARGS... -> run one function below, e.g. sh gateway.sh send "hello"',
    `# Gateway URL: ${url} (override with GATEWAY_URL)`,
    'GATEWAY_URL="${GATEWAY_URL:-' + url + '}"',
    key,
    '',
    'gw() { curl -sS -H "Authorization: Bearer $GATEWAY_KEY" -H "Content-Type: application/json" "$@"; echo; }',
    '# JSON string from $1 (escapes backslashes, quotes, newlines)',
    String.raw`js() { printf '%s' "$1" | awk 'BEGIN { printf "\"" } { gsub(/\\/, "\\\\"); gsub(/"/, "\\\""); if (NR > 1) printf "\\n"; printf "%s", $0 } END { printf "\"" }'; }`,
    '',
    '# who am I, what may I do',
    'me() { gw "$GATEWAY_URL/me"; }',
    ...fns.flatMap((f) => ['', ...f]),
    ...(c.acts.length ? [] : ['', '# no actions allowed yet: tick some for this key on the admin Permissions page']),
    '',
    'if [ "$#" -gt 0 ]; then "$@"; else me; fi',
  ].join('\n') + '\n';
}
