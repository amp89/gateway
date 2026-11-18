<script lang="ts">
  import { post, setCsrf } from '../api';
  let { done }: { done: () => void } = $props();
  let password = $state(''), error = $state(''), busy = $state(false);

  async function submit(e: Event) {
    e.preventDefault();
    busy = true; error = '';
    try { const r = await post('/login', { password }); setCsrf(r.csrf); password = ''; done(); }
    catch (err: any) { error = err.message; }
    finally { busy = false; }
  }
</script>

<form class="card narrow" onsubmit={submit}>
  <h1>Admin login</h1>
  <label><span>Password</span><input type="password" bind:value={password} autocomplete="current-password" required /></label>
  {#if error}<p class="error">{error}</p>{/if}
  <button class="primary" disabled={busy}>Log in</button>
</form>
