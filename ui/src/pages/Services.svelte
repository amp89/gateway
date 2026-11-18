<script lang="ts">
  import { get, post, patch, put, del, when } from '../api';
  import Modal from '../lib/Modal.svelte';

  const blankSvc = () => ({ name: '', base_url: 'https://', auth_style: 'bearer', auth_name: '' });
  const blankOp = () => ({ name: '', method: 'GET', path_template: '/', query_fields: '', body_fields: '', scope_fields: '', required_fields: '', description: '', anyQuery: false, anyBody: false });

  let services = $state<any[]>([]), bundled = $state<any[]>([]), errors = $state<Record<string, string>>({});
  let selected = $state<any>(null), ops = $state<any[]>([]);       // service modal
  let edit = $state({ ...blankSvc(), enabled: true }), credential = $state(''), saved = $state('');
  let adding = $state(false), svc = $state(blankSvc());              // add-service modal
  let importing = $state(false), packText = $state(''), importName = $state('');
  let addPack = $state<null | { p: any; name: string; base_url: string }>(null); // bundled pack modal
  let opForm = $state<null | { id: number | null; f: ReturnType<typeof blankOp> }>(null); // operation modal

  async function load() {
    services = (await get('/services')).services;
    bundled = (await get('/packs')).packs;
    if (selected) { selected = services.find((s) => s.id === selected.id) ?? null; if (selected) await loadOps(); }
  }
  async function loadOps() { ops = (await get(`/services/${selected.id}/operations`)).operations; }
  // Each form shows its own error next to it (where = which form).
  async function run(f: () => Promise<any>, where: string) {
    errors = {}; saved = '';
    try { await f(); await load(); return true; } catch (e: any) { errors = { [where]: e.message }; return false; }
  }

  function open(s: any) {
    selected = s; errors = {}; saved = ''; credential = '';
    edit = { name: s.name, base_url: s.base_url, auth_style: s.auth_style, auth_name: s.auth_name ?? '', enabled: s.enabled };
    ops = []; loadOps();
  }
  function close() { selected = null; opForm = null; }

  function createService(e: Event) {
    e.preventDefault();
    run(async () => { const r = await post('/services', svc); svc = blankSvc(); adding = false; open(r); }, 'svc');
  }
  function saveService(e: Event) {
    e.preventDefault();
    run(async () => { const r = await patch(`/services/${selected.id}`, edit); selected = r; saved = 'Saved'; }, 'edit');
  }
  function saveCredential(e: Event) {
    e.preventDefault();
    run(async () => { await put(`/services/${selected.id}/credential`, { credential }); credential = ''; saved = 'Credential saved'; }, 'cred');
  }
  function deleteService() {
    if (confirm(`Delete service "${selected.name}", its operations and grants?`)) run(async () => { await del(`/services/${selected.id}`); selected = null; }, 'selected');
  }
  async function exportPack() {
    const pack = await get(`/services/${selected.id}/export`);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(pack, null, 2)], { type: 'application/json' }));
    a.download = `${selected.name}.json`; a.click(); URL.revokeObjectURL(a.href);
  }

  const list = (s: string) => s.split(',').map((x) => x.trim()).filter(Boolean);
  function editOp(o: any) {
    opForm = { id: o.id, f: { name: o.name, method: o.method, path_template: o.path_template, query_fields: (o.query_fields ?? []).join(', '),
      body_fields: (o.body_fields ?? []).join(', '), scope_fields: o.scope_fields.join(', '), required_fields: (o.required_fields ?? []).join(', '), description: o.description,
      anyQuery: o.query_fields === null, anyBody: o.body_fields === null } };
  }
  function saveOp(e: Event) {
    e.preventDefault();
    if (!opForm) return;
    const { id, f } = opForm;
    const body: any = { name: f.name, method: f.method, path_template: f.path_template, scope_fields: list(f.scope_fields), required_fields: list(f.required_fields), description: f.description,
      query_fields: f.anyQuery ? null : list(f.query_fields), body_fields: f.anyBody ? null : list(f.body_fields) };
    run(async () => {
      if (id) await patch(`/operations/${id}`, { ...body, query_fields: body.query_fields ?? undefined, body_fields: body.body_fields ?? undefined,
        clear_query_fields: f.anyQuery, clear_body_fields: f.anyBody });
      else await post(`/services/${selected.id}/operations`, body);
      opForm = null;
    }, 'op');
  }
  function deleteOp(o: any) { if (confirm(`Delete ${selected.name}.${o.name} and its grants?`)) run(() => del(`/operations/${o.id}`), 'selected'); }

  // Pack name from the pasted JSON fills the name box (only while the box is empty or still that default).
  let lastDefault = '';
  function packChanged() {
    try { const n = JSON.parse(packText)?.service?.name; if (n && (!importName || importName === lastDefault)) { importName = lastDefault = n; } } catch { /* not JSON yet */ }
  }
  async function readFile(e: Event) { const f = (e.target as HTMLInputElement).files?.[0]; if (f) { packText = await f.text(); packChanged(); } }
  function importPack(e: Event) {
    e.preventDefault();
    let pack; try { pack = JSON.parse(packText); } catch { errors = { import: 'Pack is not valid JSON' }; return; }
    const q = importName.trim() ? `?name=${encodeURIComponent(importName.trim())}` : '';
    run(async () => { const r = await post(`/services/import${q}`, pack); packText = ''; importName = lastDefault = ''; importing = false; open(r); }, 'import');
  }
  function startAddPack(p: any) { errors = {}; addPack = { p, name: p.suggested_name, base_url: p.self_hosted ? '' : p.base_url }; }
  function saveAddPack(e: Event) {
    e.preventDefault();
    if (!addPack) return;
    const { p, name, base_url } = addPack;
    const body: any = { name: name.trim() };
    if (base_url.trim() && base_url.trim() !== p.base_url) body.base_url = base_url.trim();
    if (p.self_hosted) body.base_url = base_url.trim();
    run(async () => { const r = await post(`/packs/${p.file}/import`, body); addPack = null; open(r); }, 'addpack');
  }
  const fields = (v: string[] | null) => (v === null ? 'any' : v.length ? v.join(', ') : '—');
  load();
</script>

<div class="card">
  <div class="title-row">
    <h1>Services</h1>
    <span class="spacer"></span>
    <button class="primary" onclick={() => { errors = {}; adding = true; }}>Add service</button>
    <button onclick={() => { errors = {}; importing = true; }}>Import pack</button>
  </div>
  <p class="muted">Any HTTPS API: a base URL, a credential, and the operations keys may call. Each operation becomes a permission (<code>service.operation</code>) on the Permissions page. Click a service to edit it.</p>
  <div class="scroll"><table>
    <thead><tr><th>Name</th><th>Base URL</th><th>Auth</th><th>Credential</th><th>Ops</th><th>Updated</th><th></th></tr></thead>
    <tbody>
      {#each services as s (s.id)}
        <tr class="clickable" onclick={() => open(s)}>
          <td><strong>{s.name}</strong>{#if !s.enabled} <span class="pill">disabled</span>{/if}</td>
          <td class="mono">{s.base_url}</td><td>{s.auth_style}{s.auth_name ? ` (${s.auth_name})` : ''}</td>
          <td>{#if s.credential_set}set{:else}<span class="error">missing</span>{/if}</td>
          <td>{#if s.operations}{s.operations}{:else}<span class="error">0 — add one</span>{/if}</td><td>{when(s.updated_at)}</td>
          <td><button onclick={(e) => { e.stopPropagation(); open(s); }}>Edit</button></td></tr>
      {:else}<tr><td colspan="7" class="muted">No services. Add one or import a pack.</td></tr>{/each}
    </tbody>
  </table></div>
</div>

{#if bundled.length}
  <div class="card">
    <h2>Bundled packs</h2>
    <p class="muted">Ready-made service definitions shipped with the gateway. Add one as many times as you like under different names (e.g. <code>todoist</code> and <code>todoist2</code> for two accounts). Adding never changes an existing service.</p>
    <table>
      <thead><tr><th>Pack</th><th>Operations</th><th>Added as</th><th></th></tr></thead>
      <tbody>
        {#each bundled as p (p.file)}
          <tr><td>{p.service}{#if p.self_hosted} <span class="pill">self-hosted</span>{/if}</td><td>{p.operations}</td>
            <td>{p.installed_as.join(', ') || '—'}</td>
            <td><button onclick={() => startAddPack(p)}>Add…</button></td></tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

{#if adding}
  <Modal title="Add service" onclose={() => (adding = false)}>
    <form onsubmit={createService}>
      <label><span>Name: lowercase letters, digits, _ (max 32)</span><input bind:value={svc.name} required maxlength="32" pattern="[a-z0-9][a-z0-9_]*" placeholder="e.g. github" /></label>
      <label><span>Base URL: https://host/optional/path</span><input bind:value={svc.base_url} required placeholder="https://api.example.com/v1" /></label>
      <label><span>Auth</span><select bind:value={svc.auth_style}><option value="bearer">Bearer header</option><option value="header">Custom header</option><option value="query">Query parameter</option></select></label>
      {#if svc.auth_style !== 'bearer'}<label><span>{svc.auth_style === 'header' ? 'Header name, e.g. X-Api-Key' : 'Query parameter name, e.g. api_key'}</span><input bind:value={svc.auth_name} required /></label>{/if}
      <p class="muted">Next: set its credential and add at least one operation. Keys can only be given operations.</p>
      {#if errors.svc}<p class="error">{errors.svc}</p>{/if}
      <div class="dlg-foot"><button type="button" onclick={() => (adding = false)}>Cancel</button><button class="primary">Add</button></div>
    </form>
  </Modal>
{/if}

{#if importing}
  <Modal title="Import pack" onclose={() => (importing = false)}>
    <form onsubmit={importPack}>
      <label><span>Pack file (.json)</span><input type="file" accept="application/json" onchange={readFile} /></label>
      <label><span>or paste pack JSON</span><textarea rows="8" bind:value={packText} oninput={packChanged} class="mono"></textarea></label>
      <label><span>Service name (must be new; existing services are never changed)</span>
        <input bind:value={importName} maxlength="32" pattern="[a-z0-9][a-z0-9_]*" placeholder="from the pack" /></label>
      {#if errors.import}<p class="error">{errors.import}</p>{/if}
      <div class="dlg-foot"><button type="button" onclick={() => (importing = false)}>Cancel</button><button class="primary" disabled={!packText}>Import</button></div>
    </form>
  </Modal>
{/if}

{#if addPack}
  <Modal title={`Add ${addPack.p.service}`} onclose={() => (addPack = null)}>
    <form onsubmit={saveAddPack}>
      <label><span>Service name: lowercase letters, digits, _ (must be new)</span>
        <input bind:value={addPack.name} required maxlength="32" pattern="[a-z0-9][a-z0-9_]*" /></label>
      <label><span>{addPack.p.self_hosted ? `Your ${addPack.p.service} API URL (self-hosted), e.g. ${addPack.p.base_url}` : 'Base URL'}</span>
        <input bind:value={addPack.base_url} required placeholder={addPack.p.base_url} /></label>
      <p class="muted">{addPack.p.operations} operations. Next: set the credential in the service window that opens.</p>
      {#if errors.addpack}<p class="error">{errors.addpack}</p>{/if}
      <div class="dlg-foot"><button type="button" onclick={() => (addPack = null)}>Cancel</button><button class="primary">Add</button></div>
    </form>
  </Modal>
{/if}

{#if selected}
  <Modal title={`Service ${selected.name}`} wide onclose={close}>
    {#if !ops.length}<p class="error">Keys can't use this service yet: add an operation below. Each operation becomes a permission column on the Permissions page.</p>{/if}
    {#if !selected.credential_set}<p class="error">No credential yet: calls answer 502 until you set one.</p>{/if}

    <h3>Settings</h3>
    <form onsubmit={saveService}>
      <div class="row">
        <label><span>Name (renames its permissions too)</span><input bind:value={edit.name} required maxlength="32" pattern="[a-z0-9][a-z0-9_]*" /></label>
        <label style="flex:2"><span>Base URL</span><input bind:value={edit.base_url} required /></label>
      </div>
      <div class="row">
        <label><span>Auth</span><select bind:value={edit.auth_style}><option value="bearer">Bearer header</option><option value="header">Custom header</option><option value="query">Query parameter</option></select></label>
        {#if edit.auth_style !== 'bearer'}<label><span>{edit.auth_style === 'header' ? 'Header name' : 'Query parameter name'}</span><input bind:value={edit.auth_name} required /></label>{/if}
        <label style="flex:0; white-space:nowrap"><span>Enabled</span><input type="checkbox" bind:checked={edit.enabled} /></label>
        <button class="primary">Save settings</button>
      </div>
      {#if errors.edit}<p class="error">{errors.edit}</p>{/if}
    </form>

    <h3>Credential</h3>
    <form class="row" onsubmit={saveCredential}>
      <label><span>{selected.credential_set ? 'Set: type a new one to rotate it' : 'Not set'} (stored encrypted, never shown again)</span>
        <input type="password" bind:value={credential} required autocomplete="off" /></label>
      <button class="primary">{selected.credential_set ? 'Rotate' : 'Save credential'}</button>
    </form>
    {#if errors.cred}<p class="error">{errors.cred}</p>{/if}
    {#if saved}<p class="ok" role="status">{saved}</p>{/if}

    <div class="title-row"><h3>Operations</h3><span class="spacer"></span>
      <button class="primary" onclick={() => { errors = {}; opForm = { id: null, f: blankOp() }; }}>Add operation</button></div>
    <div class="scroll"><table>
      <thead><tr><th>Operation</th><th>Request</th><th>Query fields</th><th>Body fields</th><th>Scope</th><th></th></tr></thead>
      <tbody>
        {#each ops as o (o.id)}
          <tr><td>{o.name}{#if !o.enabled} <span class="pill">off</span>{/if}<div class="muted">{o.description}</div></td>
            <td class="mono">{o.method} {o.path_template}</td><td>{fields(o.query_fields)}</td><td>{fields(o.body_fields)}{#if o.required_fields?.length}<div class="muted">required: {o.required_fields.join(', ')}</div>{/if}</td><td>{o.scope_fields.join(', ') || '—'}</td>
            <td class="actions"><button onclick={() => { errors = {}; editOp(o); }}>Edit</button> <button class="danger" onclick={() => deleteOp(o)}>Delete</button></td></tr>
        {:else}<tr><td colspan="6" class="muted">No operations yet. Example: name <code>list_items</code>, method GET, path <code>/items</code>.</td></tr>{/each}
      </tbody>
    </table></div>
    {#if errors.selected}<p class="error">{errors.selected}</p>{/if}

    <div class="dlg-foot">
      <button class="danger" onclick={deleteService}>Delete service</button>
      <span class="spacer"></span>
      <button onclick={exportPack}>Export pack</button>
      <button onclick={close}>Close</button>
    </div>
  </Modal>
{/if}

{#if opForm && selected}
  <Modal title={opForm.id ? `Edit ${selected.name}.${opForm.f.name}` : `Add operation to ${selected.name}`} wide onclose={() => (opForm = null)}>
    <form onsubmit={saveOp}>
      <div class="row">
        <label><span>Name</span><input bind:value={opForm.f.name} required pattern="[a-z0-9][a-z0-9_]*" placeholder="e.g. list_items" /></label>
        <label style="flex:0"><span>Method</span><select bind:value={opForm.f.method}>{#each ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] as m}<option>{m}</option>{/each}</select></label>
        <label style="flex:2"><span>Path (placeholders like {'{task_id}'})</span><input bind:value={opForm.f.path_template} required /></label>
      </div>
      <div class="row">
        <label><span>Query fields (comma-separated)</span><input bind:value={opForm.f.query_fields} disabled={opForm.f.anyQuery} /></label>
        <label style="flex:0"><span>any</span><input type="checkbox" bind:checked={opForm.f.anyQuery} /></label>
        <label><span>Body fields</span><input bind:value={opForm.f.body_fields} disabled={opForm.f.anyBody || opForm.f.method === 'GET'} /></label>
        <label style="flex:0"><span>any</span><input type="checkbox" bind:checked={opForm.f.anyBody} disabled={opForm.f.method === 'GET'} /></label>
      </div>
      <div class="row">
        <label><span>Scope fields (path/query/body names)</span><input bind:value={opForm.f.scope_fields} /></label>
        <label><span>Required fields (calls without them are refused)</span><input bind:value={opForm.f.required_fields} /></label>
      </div>
      <div class="row">
        <label><span>Description</span><input bind:value={opForm.f.description} maxlength="200" /></label>
      </div>
      {#if errors.op}<p class="error">{errors.op}</p>{/if}
      <div class="dlg-foot"><button type="button" onclick={() => (opForm = null)}>Cancel</button><button class="primary">{opForm.id ? 'Save' : 'Add operation'}</button></div>
    </form>
  </Modal>
{/if}
