<script lang="ts">
  import { post } from '../api';
  let { done }: { done: () => void } = $props();
  let passphrase = $state(''), error = $state(''), busy = $state(false);

  async function submit(e: Event) {
    e.preventDefault();
    busy = true; error = '';
    try { await post('/unlock', { passphrase }); passphrase = ''; done(); }
    catch (err: any) { error = err.body?.retry_after ? `${err.body.error}: retry in ${err.body.retry_after}s` : err.message; }
    finally { busy = false; }
  }
</script>

<form class="card narrow" onsubmit={submit}>
  <h1>Gateway sealed</h1>
  <p class="muted">Enter the master passphrase to unlock. (Or run <code>./unlock.sh</code> on the server.)</p>
  <label><span>Passphrase</span><input type="password" bind:value={passphrase} autocomplete="current-password" required /></label>
  {#if error}<p class="error">{error}</p>{/if}
  <button class="primary" disabled={busy}>{busy ? 'Unlocking…' : 'Unlock'}</button>
</form>
