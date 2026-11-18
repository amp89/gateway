<script lang="ts">
  import { get, post, patch, del, when, copyText } from '../api';
  import Modal from '../lib/Modal.svelte';
  import Menu from '../lib/Menu.svelte';
  import { actionsFor, python, shell } from '../snippets';

  type Lang = 'python' | 'shell';
  const LANG = { python: { tab: 'Python (stdlib)', file: 'gateway_client.py' }, shell: { tab: 'curl (sh)', file: 'gateway.sh' } };
  const LANGS: Lang[] = ['python', 'shell'];

  let keys = $state<any[]>([]), name = $state(''), error = $state('');
  let fresh = $state<{ id: number; name: string; key: string } | null>(null);
  let renaming = $state<{ k: any; name: string; error: string } | null>(null);
  let viewing = $state<{ k: any; secret?: string; lang: Lang; code: Record<Lang, string>; error: string } | null>(null);
  let copied = $state<string | null>(null);           // which button shows "Copied ✓"
  let rowError = $state<Record<string, string>>({});  // per key id (or 'fresh')
  let apiUrl = $state(''), apiHosts = $state<string[]>([]);

  async function load() { keys = (await get('/keys')).keys; }
  async function run(f: () => Promise<any>) { error = ''; try { await f(); await load(); } catch (e: any) { error = e.message; } }
  async function init() {
    try { const s = await get('/settings'); apiUrl = s.api_url; apiHosts = s.api_hosts; } catch { /* shown as missing URL */ }
    await load();
  }

  function create(e: Event) {
    e.preventDefault();
    run(async () => { const r = await post('/keys', { name }); fresh = { id: r.id, name: r.name, key: r.key }; name = ''; });
  }
  function flash(id: string) { copied = id; setTimeout(() => { if (copied === id) copied = null; }, 2000); }

  async function build(k: { id: number; name: string }, secret?: string): Promise<Record<Lang, string>> {
    const ctx = { key: k.name, url: apiUrl, secret, acts: await actionsFor(k.id) };
    return { python: python(ctx), shell: shell(ctx) };
  }
  async function copyCode(k: any, lang: Lang, slot: string, secret?: string) {
    rowError = { ...rowError, [slot]: '' };
    try { await copyText((await build(k, secret))[lang]); flash(slot); }
    catch (e: any) { rowError = { ...rowError, [slot]: e.message }; }
  }
  async function view(k: any, slot: string, secret?: string) {
    rowError = { ...rowError, [slot]: '' };
    try { viewing = { k, secret, lang: 'python', code: await build(k, secret), error: '' }; }
    catch (e: any) { rowError = { ...rowError, [slot]: e.message }; }
  }
  async function copyViewed() {
    if (!viewing) return;
    const v = viewing;
    try { await copyText(v.code[v.lang]); v.error = ''; flash('view'); } catch (e: any) { v.error = e.message; }
  }
  async function copyKey() {
    if (!fresh) return;
    try { await copyText(fresh.key); flash('key'); } catch (e: any) { rowError = { ...rowError, fresh: e.message }; }
  }
  const menuItems = (k: any, slot: string, secret?: string) => [
    { label: 'Copy Python (stdlib)', run: () => copyCode(k, 'python', slot, secret) },
    { label: 'Copy curl (sh)', run: () => copyCode(k, 'shell', slot, secret) },
    { divider: true as const },
    { label: 'View code…', run: () => view(k, slot, secret) },
  ];

  function saveRename(e: Event) {
    e.preventDefault();
    if (!renaming) return;
    const r = renaming;
    if (r.name === r.k.name) { renaming = null; return; }
    patch(`/keys/${r.k.id}`, { name: r.name }).then(() => { renaming = null; load(); }).catch((err) => (r.error = err.message));
  }
  function toggle(k: any) { run(() => patch(`/keys/${k.id}`, { status: k.status === 'active' ? 'disabled' : 'active' })); }
  function revoke(k: any) { if (confirm(`Revoke "${k.name}"? This cannot be undone.`)) run(() => del(`/keys/${k.id}`)); }
  init();
</script>

<div class="card">
  <h1>Gateway keys</h1>
  <form class="row" onsubmit={create}>
    <label><span>New key name</span><input bind:value={name} required maxlength="64" /></label>
    <button class="primary">Create</button>
  </form>
  {#if fresh}
    <div class="card">
      <p><strong>{fresh.name}</strong>: copy this key now. It will not be shown again.</p>
      <p class="secret mono">{fresh.key}</p>
      <button class:ok={copied === 'key'} onclick={copyKey}>{copied === 'key' ? 'Copied ✓' : 'Copy key'}</button>
      <Menu label={copied === 'fresh' ? 'Copied ✓' : 'Code with this key'} done={copied === 'fresh'} items={menuItems(fresh, 'fresh', fresh.key)} />
      <button onclick={() => (fresh = null)}>Done</button>
      {#if rowError.fresh}<p class="error">{rowError.fresh}</p>{/if}
    </div>
  {/if}
  {#if error}<p class="error">{error}</p>{/if}
  <div class="scroll"><table>
    <thead><tr><th>Name</th><th>Key</th><th>Status</th><th>Created</th><th>Last used</th><th></th></tr></thead>
    <tbody>
      {#each keys as k (k.id)}
        {@const slot = String(k.id)}
        <tr>
          <td>{k.name}</td><td class="mono">gw_…{k.key_hint}</td><td>{k.status}</td>
          <td>{when(k.created_at)}</td><td>{when(k.last_used_at)}</td>
          <td class="actions">{#if k.status !== 'revoked'}
            <Menu label={copied === slot ? 'Copied ✓' : 'Code'} done={copied === slot} title="Client code for this key, to run on another machine" items={menuItems(k, slot)} />
            <button onclick={() => (renaming = { k, name: k.name, error: '' })}>Rename</button>
            <button onclick={() => toggle(k)}>{k.status === 'active' ? 'Disable' : 'Enable'}</button>
            <button class="danger" onclick={() => revoke(k)}>Revoke</button>
          {/if}
          {#if rowError[slot]}<div class="error">{rowError[slot]}</div>{/if}</td>
        </tr>
      {:else}<tr><td colspan="6" class="muted">No keys yet.</td></tr>{/each}
    </tbody>
  </table></div>
  <p class="muted">Code: a ready client for another machine (Python standard library, or sh + curl) with one function per action the key may use (Permissions page). It calls <code class="mono">{apiUrl || '…'}</code>.</p>
</div>

{#if renaming}
  <Modal title={`Rename key ${renaming.k.name}`} onclose={() => (renaming = null)}>
    <form onsubmit={saveRename}>
      <label><span>Name</span><input bind:value={renaming.name} required maxlength="64" /></label>
      {#if renaming.error}<p class="error">{renaming.error}</p>{/if}
      <div class="dlg-foot"><button type="button" onclick={() => (renaming = null)}>Cancel</button><button class="primary">Save</button></div>
    </form>
  </Modal>
{/if}

{#if viewing}
  <Modal title={`Client code for ${viewing.k.name}`} wide onclose={() => (viewing = null)}>
    <div class="tabs" role="tablist">
      {#each LANGS as l (l)}
        <button type="button" role="tab" aria-selected={viewing.lang === l} onclick={() => viewing && (viewing.lang = l)}>{LANG[l].tab}</button>
      {/each}
    </div>
    <p class="muted">Save as <code>{LANG[viewing.lang].file}</code> on the machine that calls the gateway.
      Gateway URL: <code>{apiUrl}</code> (first name in <code>API_HOSTS</code>{apiHosts.length > 1 ? `, of ${apiHosts.join(', ')}` : ''} + <code>API_PREFIX</code>, from <code>.env</code>; override there with <code>GATEWAY_URL</code>).
      {#if !viewing.secret}Set <code>GATEWAY_KEY</code> to this key's secret (shown once when it was created).{:else}Contains the key: keep it private.{/if}</p>
    <div role="tabpanel"><pre class="code">{viewing.code[viewing.lang]}</pre></div>
    {#if viewing.error}<p class="error">{viewing.error}</p>{/if}
    <div class="dlg-foot">
      <button class="primary" onclick={copyViewed}>{copied === 'view' ? 'Copied ✓' : 'Copy'}</button>
      <button onclick={() => (viewing = null)}>Close</button>
    </div>
  </Modal>
{/if}
