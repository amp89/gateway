<script lang="ts">
  import { get, when } from '../api';
  let rows = $state<any[]>([]), next = $state<number | null>(null), error = $state('');
  let action = $state(''), status = $state(''), keyId = $state('');
  let keys = $state<any[]>([]);

  async function load(more = false) {
    error = '';
    const q = new URLSearchParams();
    if (action) q.set('action', action);
    if (status) q.set('status', status);
    if (keyId) q.set('key_id', keyId);
    if (more && next) q.set('before_id', String(next));
    try { const r = await get(`/audit?${q}`); rows = more ? [...rows, ...r.rows] : r.rows; next = r.next_before_id; }
    catch (e: any) { error = e.message; }
  }
  get('/keys').then((r) => (keys = r.keys));
  load();
</script>

<div class="card">
  <h1>Audit log</h1>
  <form class="row" onsubmit={(e) => { e.preventDefault(); load(); }}>
    <label><span>Action</span><input bind:value={action} placeholder="e.g. telegram.send" /></label>
    <label><span>Status</span><input bind:value={status} placeholder="e.g. 403" /></label>
    <label><span>Key</span><select bind:value={keyId}><option value="">Any</option>{#each keys as k}<option value={String(k.id)}>{k.name}</option>{/each}</select></label>
    <button class="primary">Filter</button>
  </form>
  {#if error}<p class="error">{error}</p>{/if}
  <div class="scroll"><table>
    <thead><tr><th>Time</th><th>Key</th><th>Action</th><th>Resource</th><th>Status</th><th>ms</th><th>IP</th></tr></thead>
    <tbody>
      {#each rows as r (r.id)}
        <tr><td>{when(r.ts)}</td><td>{r.key_name}</td><td class="mono">{r.action}</td><td class="mono">{r.resource}</td><td>{r.status}</td><td>{r.duration_ms}</td><td class="mono">{r.client_ip}</td></tr>
      {:else}<tr><td colspan="7" class="muted">Nothing logged.</td></tr>{/each}
    </tbody>
  </table></div>
  {#if next}<button onclick={() => load(true)}>Older</button>{/if}
</div>
