<script lang="ts">
  import { get, post, patch, del } from '../api';
  import Modal from '../lib/Modal.svelte';

  type Form = { id: number | null; bot_id: string; command: string; key_id: string; action: string; call: string; reply: string; enabled: boolean };
  const blank = (): Form => ({ id: null, bot_id: '', command: '', key_id: '', action: '', call: '', reply: '', enabled: true });

  let rules = $state<any[]>([]), bots = $state<any[]>([]), keys = $state<any[]>([]), actions = $state<any[]>([]);
  let error = $state(''), formError = $state(''), form = $state<Form | null>(null), editing = $state<any>(null);
  let sample = $state('buy milk'), dry = $state<any>(null), dryError = $state('');
  // Template last written by "Fill template": replaced when the operation changes, a hand-edited one is kept.
  let filled = '';
  const op = $derived(form ? actions.find((a: any) => a.action === form!.action) : undefined);

  async function load() {
    try {
      const [r, b, k, a] = await Promise.all([get('/commands'), get('/bots'), get('/keys'), get('/commands/operations')]);
      rules = r.commands; bots = b.bots; keys = k.keys.filter((x: any) => x.status !== 'revoked');
      actions = a.operations;
    } catch (e: any) { error = e.message; }
  }
  /** Every required field, {text} in the first one that is a plain value (not a scope or path field). */
  function skeleton(o: any): string {
    const req: string[] = o.required_fields || [];
    const where = (f: string) => o.path_fields.includes(f) ? 'path' : (o.body_fields || []).includes(f) ? 'body' : 'query';
    const textField = req.find((f) => !o.scope_fields.includes(f) && !o.path_fields.includes(f)) ?? req[0] ?? o.path_fields[0];
    const call: any = { path: {}, query: {}, body: {} };
    for (const f of [...o.path_fields, ...req.filter((f) => !o.path_fields.includes(f))]) call[where(f)][f] = f === textField ? '{text}' : '';
    if (o.method === 'GET' || o.method === 'DELETE') delete call.body;
    return JSON.stringify(call, null, 2);
  }
  function fillTemplate() { if (form && op) { form.call = filled = skeleton(op); dry = null; } }
  function actionChanged() { if (form && op && (!form.call.trim() || form.call === filled)) fillTemplate(); }
  function edit(r: any) {
    formError = ''; dry = null; dryError = ''; editing = r; filled = '';
    form = { id: r.id, bot_id: String(r.bot_id), command: r.command, key_id: String(r.key_id), action: r.action,
      call: JSON.stringify(r.call, null, 2), reply: r.reply, enabled: r.enabled };
  }
  function add() { formError = ''; dry = null; dryError = ''; editing = null; filled = ''; form = blank(); }
  function payload() {
    if (!form) return null;
    let call: any;
    try { call = JSON.parse(form.call || '{}'); } catch { formError = 'Call template is not valid JSON'; return null; }
    return { bot_id: Number(form.bot_id), command: form.command.trim().replace(/^\//, '').toLowerCase(),
      key_id: Number(form.key_id), action: form.action, call, reply: form.reply, enabled: form.enabled };
  }
  async function dryRun() {
    formError = ''; dry = null; dryError = '';
    const body = payload(); if (!body) return;
    try { dry = await post('/commands/dry-run', { ...body, text: sample }); } catch (err: any) { dryError = err.message; }
  }
  async function save(e: Event) {
    e.preventDefault();
    if (!form) return;
    formError = '';
    const body = payload(); if (!body) return;
    try {
      const r = form.id === null ? await post('/commands', body) : await patch(`/commands/${form.id}`, body);
      form = null; await load();
      if (r.problem) error = `/${r.command} saved, but it won't run yet: ${r.problem}`;
    } catch (err: any) { formError = err.message; }
  }
  async function toggle(r: any) {
    error = '';
    try { await patch(`/commands/${r.id}`, { enabled: !r.enabled }); await load(); } catch (e: any) { error = e.message; }
  }
  async function remove(r: any) {
    if (!confirm(`Delete /${r.command} on ${r.bot}?`)) return;
    error = '';
    try { await del(`/commands/${r.id}`); await load(); } catch (e: any) { error = e.message; }
  }
  load();
</script>

<div class="card">
  <h1>Bot commands</h1>
  <p class="muted">Someone on a bot's allowlist sends <span class="mono">/todo buy milk</span> in a private chat: the gateway runs one service operation as a key, then replies. The key can only do what is ticked for it on Permissions. Commands are stored like any other message.</p>
  {#if error}<p class="error">{error}</p>{/if}
  <div class="scroll"><table>
    <thead><tr><th>Bot</th><th>Command</th><th>Runs</th><th>As key</th><th>Reply</th><th>On</th><th></th></tr></thead>
    <tbody>
      {#each rules as r (r.id)}
        <tr>
          <td>{r.bot}</td><td class="mono">/{r.command}</td>
          <td><span class="mono">{r.action}</span>{#if r.problem}<br /><span class="error">{r.problem}</span>{/if}
            {#if r.note}<br /><span class="muted">{r.note}</span>{/if}
            {#if r.last_error}<br /><span class="error">Last run failed {new Date(r.last_error_at * 1000).toLocaleString()}: {r.last_error}</span>{/if}</td>
          <td>{r.key}</td><td class="muted">{r.reply ? 'template' : 'done / short error'}</td>
          <td><input type="checkbox" checked={r.enabled} onchange={() => toggle(r)} aria-label={`Enable /${r.command}`} /></td>
          <td><button onclick={() => edit(r)}>Edit</button> <button class="danger" onclick={() => remove(r)}>Delete</button></td>
        </tr>
      {:else}<tr><td colspan="7" class="muted">No commands yet.</td></tr>{/each}
    </tbody>
  </table></div>
  <button class="primary" onclick={add} disabled={!bots.length || !keys.length || !actions.length}>Add command</button>
  {#if !bots.length || !keys.length || !actions.length}<p class="muted">Needs a bot, a key and a service operation first.</p>{/if}
</div>

{#if form}
  <Modal title={form.id === null ? 'Add command' : `Edit /${form.command}`} onclose={() => (form = null)} wide>
    <form onsubmit={save}>
      <div class="row">
        <label><span>Bot</span><select bind:value={form.bot_id} required><option value="" disabled>choose</option>{#each bots as b}<option value={String(b.id)}>{b.name}</option>{/each}</select></label>
        <label><span>Command (a-z, 0-9, _)</span><input bind:value={form.command} required maxlength="33" pattern="/?[A-Za-z0-9_]+" placeholder="todo" /></label>
      </div>
      <div class="row">
        <label><span>Operation</span><select bind:value={form.action} onchange={actionChanged} required><option value="" disabled>choose</option>{#each actions as a}<option value={a.action}>{a.action}</option>{/each}</select></label>
        <label><span>Run as key</span><select bind:value={form.key_id} required><option value="" disabled>choose</option>{#each keys as k}<option value={String(k.id)}>{k.name}{k.status !== 'active' ? ` (${k.status})` : ''}</option>{/each}</select></label>
      </div>
      {#if editing?.problem}<p class="error">Won't run: {editing.problem}</p>{/if}
      {#if editing?.last_error}<p class="error">Last run failed: {editing.last_error}</p>{/if}
      {#if op}
        <div class="hint" data-testid="op-fields">
          <p class="muted">{op.method} {op.path_template}{op.description ? ` — ${op.description}` : ''}</p>
          {#each [['path', op.path_fields], ['query', op.query_fields], ['body', op.body_fields]] as [part, fields]}
            {#if fields === null}<p class="muted"><b>{part}:</b> any field</p>
            {:else if fields.length}<p class="muted"><b>{part}:</b>
              {#each fields as f, i}{i ? ', ' : ' '}<span class="mono">{f}</span>{#if part === 'path' || op.required_fields.includes(f)}<b title="required">*</b>{/if}{#if op.scope_fields.includes(f)}<span title="keys can be limited by this field"> (limitable)</span>{/if}{/each}</p>{/if}
          {/each}
          <p class="muted">* required. Limitable: a key limited by this field must get a value it is allowed to use.</p>
        </div>
      {/if}
      <label><span>Call template (JSON: path, query, body). <span class="mono">{'{text}'}</span> = everything after the command.</span>
        <textarea rows="6" class="mono" bind:value={form.call} placeholder="choose an operation first"></textarea></label>
      {#if op}<button type="button" onclick={fillTemplate}>Fill template</button>{/if}
      <label><span>Reply template (empty = "done" or a short error). <span class="mono">{'{body.content}'}</span> = a field of the response; <span class="mono">{'{#body.results}- {content} {/body.results}'}</span> = one line per item.</span>
        <textarea rows="3" class="mono" bind:value={form.reply} maxlength="4096"></textarea></label>
      <label style="display:inline"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
      <div class="row">
        <label><span>Try with text</span><input bind:value={sample} maxlength="4096" /></label>
        <div style="align-self:flex-end"><button type="button" onclick={dryRun} disabled={!form.action || !form.bot_id || !form.key_id}>Dry run (sends nothing)</button></div>
      </div>
      {#if dryError}<p class="error" data-testid="dry-error">{dryError}</p>{/if}
      {#if dry}
        <div data-testid="dry-result">
          <p class="muted">Would send: <span class="mono">{dry.method} {dry.url}</span></p>
          {#if Object.keys(dry.query).length}<pre class="mono">query {JSON.stringify(dry.query, null, 2)}</pre>{/if}
          {#if dry.body !== null}<pre class="mono">{JSON.stringify(dry.body, null, 2)}</pre>{/if}
          {#if dry.problem}<p class="error">Won't run yet: {dry.problem}</p>{:else}<p class="muted">Passes every check the gateway makes.</p>{/if}
          {#if dry.note}<p class="muted">{dry.note}</p>{/if}
        </div>
      {/if}
      {#if formError}<p class="error">{formError}</p>{/if}
      <div class="dlg-foot"><button type="button" onclick={() => (form = null)}>Cancel</button><button class="primary">Save</button></div>
    </form>
  </Modal>
{/if}
