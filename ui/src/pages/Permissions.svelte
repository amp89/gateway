<script lang="ts">
  import { get, put, del } from '../api';
  import Modal from '../lib/Modal.svelte';
  let keys = $state<any[]>([]), actions = $state<any[]>([]), grants = $state<Record<string, any>>({});
  let filter = $state(''), error = $state('');
  let editing = $state<{ key: any; action: any; values: Record<string, string>; picked: Record<string, string[]>; error?: string } | null>(null);
  let bots = $state<any[]>([]), users = $state<{ id: number; label: string }[]>([]);
  let hidden = $state<{ name: string; why: string }[]>([]);
  let h1 = $state(0); // height of header row 1: row 2 sticks right below it
  let kw = $state(0); // Key column width: service names stick right of it while scrolling sideways

  // Scope fields with a friendly label; `pick` fields get checkboxes from real data instead of a text box.
  const FIELDS: Record<string, { label: string; hint?: string; pick?: 'bots' | 'users' }> = {
    bots: { label: 'Only these bots', pick: 'bots' },
    chat_ids: { label: 'Only these chats (Telegram users)', pick: 'users' },
    project_id: { label: 'Only these Todoist projects', hint: 'Project IDs, comma-separated. Todoist web: open the project, the ID is the last part of the URL.' },
  };
  const fieldLabel = (f: string) => FIELDS[f]?.label ?? `Only these values of "${f}"`;
  // What "no limit" means for an action: the key is still required; only this key's extra limit goes away.
  function noLimitText(a: any): string {
    if (a.name === 'telegram.send') return "No limit = this key may send from any bot, but only to chats on that bot's allowlist (Bots page).";
    if (a.name === 'telegram.read') return 'No limit = this key may read the stored messages of any bot (only messages from allowlisted users are stored).';
    const nouns = (a.scope_fields as string[]).map((f) => FIELDS[f]?.label.replace(/^Only these /, '') ?? `"${f}" values`);
    return `No limit = this key may use any ${nouns.join(' / ')} the service credential can reach.`;
  }
  const options = (f: string): { value: string; label: string }[] =>
    FIELDS[f]?.pick === 'bots' ? bots.map((b) => ({ value: b.name, label: b.name }))
    : FIELDS[f]?.pick === 'users' ? users.map((u) => ({ value: String(u.id), label: u.label ? `${u.label} (${u.id})` : String(u.id) }))
    : [];

  const id = (k: number, a: string) => `${k}|${a}`;
  async function load() {
    const [m, a] = await Promise.all([get('/permissions'), get('/actions')]);
    keys = m.keys; actions = a.actions;
    grants = Object.fromEntries(m.grants.map((g: any) => [id(g.key_id, g.action), g.scope ?? null]));
    if (actions.some((x: any) => x.name.startsWith('telegram.'))) {
      try { const b = await get('/bots'); bots = b.bots; users = b.users; } catch { /* no telegram module */ }
    }
    try {
      hidden = (await get('/services')).services
        .filter((x: any) => !x.enabled || !x.operations)
        .map((x: any) => ({ name: x.name, why: !x.enabled ? 'disabled' : 'no operations yet' }));
    } catch { hidden = []; }
  }
  const f = $derived(filter.trim().toLowerCase());
  const shownActions = $derived(actions.filter((a) => !f || a.name.includes(f) || keys.some((k) => k.name.toLowerCase().includes(f))));
  const shownKeys = $derived(keys.filter((k) => !f || k.name.toLowerCase().includes(f) || actions.some((a) => a.name.includes(f))));
  // Column groups: "todoist.tasks_add" -> group "todoist", column "tasks_add"
  const groups = $derived.by(() => {
    const out: { name: string; count: number }[] = [];
    for (const a of shownActions) {
      const g = a.name.split('.')[0];
      if (out.length && out[out.length - 1].name === g) out[out.length - 1].count++;
      else out.push({ name: g, count: 1 });
    }
    return out;
  });
  const short = (name: string) => name.slice(name.indexOf('.') + 1);

  async function toggle(k: any, a: any) {
    error = '';
    const g = id(k.id, a.name);
    try {
      if (g in grants) { await del(`/permissions/${k.id}/${a.name}`); const { [g]: _, ...rest } = grants; grants = rest; }
      else { await put(`/permissions/${k.id}/${a.name}`, { scope: null }); grants = { ...grants, [g]: null }; }
    } catch (e: any) { error = e.message; }
  }
  function editScope(k: any, a: any) {
    const cur = grants[id(k.id, a.name)] ?? {};
    editing = { key: k, action: a,
      values: Object.fromEntries(a.scope_fields.map((s: string) => [s, (cur[s] ?? []).join(', ')])),
      picked: Object.fromEntries(a.scope_fields.map((s: string) => [s, [...(cur[s] ?? [])]])) };
  }
  function togglePick(field: string, value: string, on: boolean) {
    if (!editing) return;
    const cur = editing.picked[field] ?? [];
    editing.picked[field] = on ? [...cur, value] : cur.filter((x) => x !== value);
  }
  async function putScope(scope: Record<string, string[]> | null) {
    if (!editing) return;
    try {
      const r = await put(`/permissions/${editing.key.id}/${editing.action.name}`, { scope });
      grants = { ...grants, [id(editing.key.id, editing.action.name)]: r.scope };
      editing = null;
    } catch (e: any) { if (editing) editing.error = e.message; }
  }
  function saveScope() {
    if (!editing) return;
    const scope: Record<string, string[]> = {};
    for (const field of editing.action.scope_fields as string[]) {
      const list = FIELDS[field]?.pick ? editing.picked[field] ?? []
        : (editing.values[field] ?? '').split(',').map((x) => x.trim()).filter(Boolean);
      if (list.length) scope[field] = list;
    }
    putScope(Object.keys(scope).length ? scope : null);
  }
  const cellState = (g: string) => (!(g in grants) ? 'off' : grants[g] ? 'limited' : 'all');
  const scopeText = (s: any) => (s ? Object.entries(s).map(([k, v]: any) => `${fieldLabel(k)}: ${v.join(', ')}`).join('; ') : '');
  load();
</script>

<div class="card">
  <h1>Permissions</h1>
  <p class="muted">Rows are keys, columns are what a key may do. Click a box to allow or disallow it.</p>
  <div class="legend">
    <span><span class="perm off" aria-hidden="true"></span> not allowed</span>
    <span><span class="perm all" aria-hidden="true">{@render check()}</span> allowed</span>
    <span><span class="perm limited" aria-hidden="true">{@render check()}</span> allowed, limited to some bots / chats / projects (hover a box, then “limit”)</span>
  </div>
  <label><span>Filter keys or actions</span><input bind:value={filter} placeholder="type to filter" /></label>
  {#if error}<p class="error">{error}</p>{/if}
  <div class="matrix-wrap"><table class="matrix" style:--h1="{h1}px" style:--kw="{kw}px">
    <thead>
      <tr bind:clientHeight={h1}><th class="key" rowspan="2" bind:clientWidth={kw}>Key</th>{#each groups as g, i (i)}<th class="grp" colspan={g.count}><span>{g.name}</span></th>{/each}</tr>
      <tr>{#each shownActions as a (a.name)}<th class="op" title={a.description}>{short(a.name).replaceAll('_', ' ')}</th>{/each}</tr>
    </thead>
    <tbody>
      {#each shownKeys as k (k.id)}
        <tr>
          <td class="key" title={k.name}>{k.name}{#if k.status !== 'active'} <span class="pill">{k.status}</span>{/if}</td>
          {#each shownActions as a (a.name)}
            {@const g = id(k.id, a.name)}
            {@const st = cellState(g)}
            {@const tip = st === 'off' ? `${k.name}: ${a.name} not allowed` : st === 'all' ? `${k.name}: ${a.name} allowed` : `${k.name}: ${a.name} allowed. ${scopeText(grants[g])}`}
            <td class="cell">
              <button class="perm {st}" onclick={() => toggle(k, a)} title={tip} aria-label={tip} aria-pressed={st !== 'off'}>
                {#if st !== 'off'}{@render check()}{/if}
              </button>
              {#if st !== 'off' && a.scope_fields.length}
                <a class="lim" class:shown={st === 'limited'} href={'#/permissions'} onclick={(e) => { e.preventDefault(); editScope(k, a); }}>limit</a>
              {/if}
            </td>
          {/each}
        </tr>
      {:else}<tr><td class="muted">No keys yet. Create one on the Keys page.</td></tr>{/each}
    </tbody>
  </table></div>
  {#if hidden.length}
    <p class="muted">Not shown here: {#each hidden as h, i}{i ? ', ' : ''}<strong>{h.name}</strong> ({h.why}){/each}. A service gets columns once it has enabled operations: <a href="#/services">Services</a>.</p>
  {/if}
</div>

{#snippet check()}<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" /></svg>{/snippet}

{#if editing}
  <Modal title={`Limit ${editing.key.name}: ${editing.action.name}`} onclose={() => (editing = null)}>
    <p class="muted">{editing.action.description}. Nothing picked in a section = no limit there.</p>
    {#each editing.action.scope_fields as field}
      <div style="margin: .6rem 0">
        <strong>{fieldLabel(field)}</strong>
        {#if FIELDS[field]?.pick}
          <div>
            {#each options(field) as o (o.value)}
              <label style="display:inline; margin-right:1rem"><input type="checkbox" checked={(editing.picked[field] ?? []).includes(o.value)}
                onchange={(e) => togglePick(field, o.value, (e.target as HTMLInputElement).checked)} /> {o.label}</label>
            {:else}<span class="muted">{FIELDS[field].pick === 'bots' ? 'No bots yet (Bots page).' : 'No Telegram users yet (Bots page → Telegram users).'}</span>{/each}
          </div>
          {#each (editing.picked[field] ?? []).filter((v) => !options(field).some((o) => o.value === v)) as stale}
            <div class="muted">Also saved: {stale} (no longer exists) <button onclick={() => togglePick(field, stale, false)}>remove</button></div>
          {/each}
        {:else}
          <input bind:value={editing.values[field]} placeholder="e.g. 6Xm5HVVRcX00, 6Xm5HVVRcX01" />
          {#if FIELDS[field]?.hint}<div class="muted">{FIELDS[field].hint}</div>{/if}
        {/if}
      </div>
    {/each}
    <p class="muted no-limit">{noLimitText(editing.action)}</p>
    {#if editing.error}<p class="error">{editing.error}</p>{/if}
    <div class="dlg-foot">
      <button type="button" onclick={() => putScope(null)}>Remove limit</button>
      <span class="spacer"></span>
      <button type="button" onclick={() => (editing = null)}>Cancel</button>
      <button type="button" class="primary" onclick={saveScope}>Save</button>
    </div>
  </Modal>
{/if}
