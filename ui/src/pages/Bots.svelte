<script lang="ts">
  import { get, post, patch, put, del, when } from '../api';
  let bots = $state<any[]>([]), users = $state<{ id: number; label: string }[]>([]), error = $state('');
  let name = $state(''), token = $state('');
  let newId = $state(''), newLabel = $state('');
  let allow = $state<Record<number, number[]>>({});

  async function load() {
    const r = await get('/bots');
    bots = r.bots; users = r.users;
    allow = Object.fromEntries(r.bots.map((b: any) => [b.id, [...b.allowlist]]));
  }
  const userLabel = (id: number) => users.find((u) => u.id === id)?.label || String(id);
  function addUser(e: Event) {
    e.preventDefault();
    const id = Number(newId.trim());
    if (!Number.isInteger(id) || id <= 0) { error = 'User ID must be a positive number'; return; }
    run(async () => { await put('/telegram/users', { users: [...users, { id, label: newLabel.trim() }] }); newId = ''; newLabel = ''; });
  }
  function removeUser(u: { id: number; label: string }) {
    if (confirm(`Remove ${u.label || u.id}? They are removed from every bot's allowlist.`))
      run(() => put('/telegram/users', { users: users.filter((x) => x.id !== u.id) }));
  }
  function toggleAllow(b: any, id: number, on: boolean) {
    const cur = allow[b.id] ?? [];
    allow[b.id] = on ? [...cur, id] : cur.filter((x) => x !== id);
  }
  async function run(f: () => Promise<any>) { error = ''; try { await f(); await load(); } catch (e: any) { error = e.message; } }
  function create(e: Event) { e.preventDefault(); run(async () => { await post('/bots', { name, token }); name = ''; token = ''; }); }
  function saveAllow(b: any) { run(() => put(`/bots/${b.id}/allowlist`, { user_ids: allow[b.id] ?? [] })); }
  function setToken(b: any) { const t = prompt(`New token for ${b.name}`); if (t) run(() => patch(`/bots/${b.id}`, { token: t })); }
  function setRetention(b: any) { const d = prompt('Keep messages for N days (0 = forever)', String(b.retention_days)); if (d !== null) run(() => patch(`/bots/${b.id}`, { retention_days: Number(d) })); }
  function remove(b: any) { if (confirm(`Delete bot "${b.name}" and its stored messages?`)) run(() => del(`/bots/${b.id}`)); }
  load();
</script>

<div class="card">
  <h1>Telegram users</h1>
  <p class="muted">People your bots may talk to. Each bot picks from this list. Find your ID by messaging <a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer">@userinfobot</a>.</p>
  <table>
    <thead><tr><th>User ID</th><th>Label</th><th></th></tr></thead>
    <tbody>
      {#each users as u (u.id)}
        <tr><td class="mono">{u.id}</td><td>{u.label}</td><td><button class="danger" onclick={() => removeUser(u)}>Remove</button></td></tr>
      {:else}<tr><td colspan="3" class="muted">No users yet. Add yourself first.</td></tr>{/each}
    </tbody>
  </table>
  <form class="row" onsubmit={addUser}>
    <label><span>User ID</span><input bind:value={newId} required inputmode="numeric" pattern="[0-9]+" /></label>
    <label><span>Label (optional)</span><input bind:value={newLabel} maxlength="64" placeholder="e.g. me" /></label>
    <button class="primary">Add user</button>
  </form>
</div>

<div class="card">
  <h1>Telegram bots</h1>
  <form class="row" onsubmit={create}>
    <label><span>Name (a-z, 0-9, _ -)</span><input bind:value={name} required pattern="[a-z0-9][a-z0-9_\-]*" /></label>
    <label><span>Bot token</span><input type="password" bind:value={token} required autocomplete="off" /></label>
    <button class="primary">Add bot</button>
  </form>
  {#if error}<p class="error">{error}</p>{/if}
</div>

{#each bots as b (b.id)}
  <div class="card">
    <h2>{b.name} <span class="muted">· added {when(b.created_at)}</span></h2>
    {#if b.poll_error}<p class="error">Polling stopped: {b.poll_error}. Set a new token, then re-enable polling.</p>{/if}
    <p><strong>May talk to:</strong>
      {#each users as u (u.id)}
        <label style="display:inline; margin-right:1rem"><input type="checkbox" checked={(allow[b.id] ?? []).includes(u.id)}
          onchange={(e) => toggleAllow(b, u.id, (e.target as HTMLInputElement).checked)} /> {u.label || u.id}</label>
      {:else}<span class="muted">Add yourself under Telegram users first.</span>{/each}
      {#if users.length}<button onclick={() => saveAllow(b)}>Save allowlist</button>{/if}
    </p>
    {#if b.allowlist.length}<p class="muted">Saved: {b.allowlist.map(userLabel).join(', ')}</p>{/if}
    <p>
      <label style="display:inline"><input type="checkbox" checked={b.polling_enabled} onchange={(e) => run(() => patch(`/bots/${b.id}`, { polling_enabled: (e.target as HTMLInputElement).checked }))} /> Poll for messages</label>
      · retention {b.retention_days ? `${b.retention_days} days` : 'forever'} <button onclick={() => setRetention(b)}>Change</button>
    </p>
    <button onclick={() => setToken(b)}>Replace token</button>
    <button onclick={() => { const n = prompt('New name', b.name); if (n && n !== b.name) run(() => patch(`/bots/${b.id}`, { name: n })); }}>Rename</button>
    <button class="danger" onclick={() => remove(b)}>Delete</button>
  </div>
{/each}
