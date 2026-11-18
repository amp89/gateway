<script lang="ts">
  import { get, post, patch, del } from '../api';
  import Modal from '../lib/Modal.svelte';

  type Form = { id: number | null; bot_id: string; command: string; key_id: string; action: string; call: string; reply: string; enabled: boolean };
  const blank = (): Form => ({ id: null, bot_id: '', command: '', key_id: '', action: '', call: '{\n  "body": {"text": "{text}"}\n}', reply: '', enabled: true });

  let rules = $state<any[]>([]), bots = $state<any[]>([]), keys = $state<any[]>([]), actions = $state<any[]>([]);
  let error = $state(''), formError = $state(''), form = $state<Form | null>(null);

  async function load() {
    try {
      const [r, b, k, a] = await Promise.all([get('/commands'), get('/bots'), get('/keys'), get('/actions')]);
      rules = r.commands; bots = b.bots; keys = k.keys.filter((x: any) => x.status !== 'revoked');
      actions = a.actions.filter((x: any) => x.source === 'service');
    } catch (e: any) { error = e.message; }
  }
  function edit(r: any) {
    formError = '';
    form = { id: r.id, bot_id: String(r.bot_id), command: r.command, key_id: String(r.key_id), action: r.action,
      call: JSON.stringify(r.call, null, 2), reply: r.reply, enabled: r.enabled };
  }
  function add() { formError = ''; form = blank(); }
  async function save(e: Event) {
    e.preventDefault();
    if (!form) return;
    let call: any;
    try { call = JSON.parse(form.call || '{}'); } catch { formError = 'Call template is not valid JSON'; return; }
    const body = { bot_id: Number(form.bot_id), command: form.command.trim().replace(/^\//, '').toLowerCase(),
      key_id: Number(form.key_id), action: form.action, call, reply: form.reply, enabled: form.enabled };
    formError = '';
    try {
      if (form.id === null) await post('/commands', body); else await patch(`/commands/${form.id}`, body);
      form = null; await load();
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
          <td><span class="mono">{r.action}</span>{#if r.problem}<br /><span class="error">{r.problem}</span>{/if}</td>
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
        <label><span>Operation</span><select bind:value={form.action} required><option value="" disabled>choose</option>{#each actions as a}<option value={a.name}>{a.name}</option>{/each}</select></label>
        <label><span>Run as key</span><select bind:value={form.key_id} required><option value="" disabled>choose</option>{#each keys as k}<option value={String(k.id)}>{k.name}{k.status !== 'active' ? ` (${k.status})` : ''}</option>{/each}</select></label>
      </div>
      <label><span>Call template (JSON: path, query, body). <span class="mono">{'{text}'}</span> = everything after the command.</span>
        <textarea rows="6" class="mono" bind:value={form.call}></textarea></label>
      <label><span>Reply template (empty = "done" or a short error). <span class="mono">{'{body.content}'}</span> = a field of the response; <span class="mono">{'{#body.results}- {content} {/body.results}'}</span> = one line per item.</span>
        <textarea rows="3" class="mono" bind:value={form.reply} maxlength="4096"></textarea></label>
      <label style="display:inline"><input type="checkbox" bind:checked={form.enabled} /> Enabled</label>
      {#if formError}<p class="error">{formError}</p>{/if}
      <div class="dlg-foot"><button type="button" onclick={() => (form = null)}>Cancel</button><button class="primary">Save</button></div>
    </form>
  </Modal>
{/if}
