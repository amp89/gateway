<script lang="ts">
  import { get, put, post, when } from '../api';
  let s = $state<any>(null), retention = $state(0), msg = $state(''), error = $state('');
  let pw = $state({ current: '', new: '' }), pp = $state({ current: '', new: '', again: '' });

  async function load() { s = await get('/settings'); retention = s.audit_retention_days; }
  async function act(f: () => Promise<any>, ok: string) { error = ''; msg = ''; try { await f(); msg = ok; await load(); } catch (e: any) { error = e.message; } }
  function cleanupNow() { act(() => post('/cleanup'), 'Cleanup done.'); }
  const days = (n: number) => (n === 0 ? 'kept forever' : `${n} day${n === 1 ? '' : 's'}`);
  function saveRetention(e: Event) { e.preventDefault(); act(() => put('/settings', { audit_retention_days: Number(retention) }), 'Saved.'); }
  function changePassword(e: Event) { e.preventDefault(); act(async () => { await post('/password', pw); pw = { current: '', new: '' }; }, 'Admin password changed. Other sessions ended.'); }
  function changePassphrase(e: Event) {
    e.preventDefault();
    if (pp.new !== pp.again) { error = 'New passphrases differ'; return; }
    act(async () => { await post('/passphrase', { current: pp.current, new: pp.new }); pp = { current: '', new: '', again: '' }; }, 'Passphrase changed; every secret was re-encrypted.');
  }
  load();
</script>

{#if msg}<p class="ok">{msg}</p>{/if}
{#if error}<p class="error">{error}</p>{/if}
{#if s}
  <div class="card">
    <h1>Settings</h1>
    <h2>Audit log</h2>
    <p class="muted">The audit log (Audit page) records every API call and admin change: who, what, when, result. Never message text or tokens.</p>
    <form class="row" onsubmit={saveRetention}>
      <label><span>Delete audit log entries older than (days, 0 = keep forever)</span><input type="number" min="0" bind:value={retention} /></label>
      <button class="primary">Save</button>
    </form>
    <p class="muted">Currently: {days(s.audit_retention_days)}.</p>
  </div>
  <div class="card">
    <h2>Cleanup</h2>
    <p class="muted">Runs every {Math.round(s.cleanup.every_s / 60)} minutes, also while sealed. Deletes audit log entries older than the setting above, and stored Telegram messages older than each bot's retention (Bots page).</p>
    {#if s.cleanup.last_run_at}
      <p>Last run {when(s.cleanup.last_run_at)}: {s.cleanup.last_messages_deleted} messages, {s.cleanup.last_audit_deleted} audit entries deleted.</p>
    {:else}<p class="muted">Not run yet since the last restart.</p>{/if}
    <button onclick={cleanupNow}>Run now</button>
  </div>
  <div class="card">
    <h2>Fixed in .env</h2>
    <p class="muted">Edit <code>.env</code> on the server and restart to change these.</p>
    <table>
      <tbody>
        <tr><td>API prefix</td><td><code>/{s.api_prefix}/v1/…</code></td><td class="muted">start of every client URL</td></tr>
        <tr><td>Rate limit</td><td>{s.api_rate_per_min} calls/min per key</td><td class="muted">more gets <code>429</code></td></tr>
        <tr><td>Message retention for new bots</td><td>{days(s.default_message_retention_days)}</td><td class="muted">change per bot on the Bots page</td></tr>
      </tbody>
    </table>
  </div>
{/if}
<div class="card">
  <h2>Admin password</h2>
  <form class="row" onsubmit={changePassword}>
    <label><span>Current</span><input type="password" bind:value={pw.current} required autocomplete="current-password" /></label>
    <label><span>New (10+)</span><input type="password" bind:value={pw.new} required minlength="10" autocomplete="new-password" /></label>
    <button class="primary">Change</button>
  </form>
</div>
<div class="card">
  <h2>Master passphrase</h2>
  <p class="muted">Re-encrypts every stored secret in one transaction. Keep the new passphrase safe: it cannot be recovered.</p>
  <form class="row" onsubmit={changePassphrase}>
    <label><span>Current</span><input type="password" bind:value={pp.current} required /></label>
    <label><span>New (12+)</span><input type="password" bind:value={pp.new} required minlength="12" /></label>
    <label><span>Repeat new</span><input type="password" bind:value={pp.again} required /></label>
    <button class="primary">Change</button>
  </form>
</div>
