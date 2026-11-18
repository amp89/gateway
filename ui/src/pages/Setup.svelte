<script lang="ts">
  import { post } from '../api';
  let { done }: { done: () => void } = $props();
  let code = $state(codeFromLink()), passphrase = $state(''), passphrase2 = $state(''), password = $state('');
  let error = $state(''), busy = $state(false);

  // The start script prints a link ending in #setup-code=…: the code never reaches the server in the URL.
  // Read it once, then drop it from the address bar and history.
  function codeFromLink(): string {
    const m = location.hash.match(/(?:^#|&)setup-code=([^&]+)/);
    if (!m) return '';
    history.replaceState(null, '', location.pathname + location.search);
    return decodeURIComponent(m[1]);
  }

  async function submit(e: Event) {
    e.preventDefault();
    if (passphrase !== passphrase2) { error = 'Passphrases differ'; return; }
    busy = true; error = '';
    try { await post('/setup', { setup_code: code.trim(), passphrase, admin_password: password }); done(); }
    catch (err: any) { error = err.message; }
    finally { busy = false; }
  }
</script>

<form class="card narrow" onsubmit={submit}>
  <h1>First run</h1>
  <p class="muted">The passphrase encrypts every stored secret. It is never saved: if you lose it, the stored tokens and messages cannot be recovered. You will enter it after every restart.</p>
  <label><span>Setup code</span><input bind:value={code} autocomplete="off" spellcheck="false" required /></label>
  <p class="muted hint">Printed by <code>podman-start-prod.sh</code> (open its link to fill this in). Or on the host: <code>podman exec gateway cat /run/gateway/setup.code</code></p>
  <label><span>Master passphrase (12+ chars)</span><input type="password" bind:value={passphrase} autocomplete="new-password" required minlength="12" /></label>
  <label><span>Repeat passphrase</span><input type="password" bind:value={passphrase2} autocomplete="new-password" required /></label>
  <label><span>Admin password (10+ chars)</span><input type="password" bind:value={password} autocomplete="new-password" required minlength="10" /></label>
  {#if error}<p class="error">{error}</p>{/if}
  <button class="primary" disabled={busy}>{busy ? 'Setting up…' : 'Set up'}</button>
</form>

<style>
  .hint { font-size: .8rem; margin: -.2rem 0 .6rem; }
</style>
