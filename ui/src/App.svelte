<script lang="ts">
  import { get, post, setCsrf, onUnauthorized } from './api';
  import Setup from './pages/Setup.svelte';
  import Unlock from './pages/Unlock.svelte';
  import Login from './pages/Login.svelte';
  import Keys from './pages/Keys.svelte';
  import Permissions from './pages/Permissions.svelte';
  import Bots from './pages/Bots.svelte';
  import Commands from './pages/Commands.svelte';
  import Services from './pages/Services.svelte';
  import Messages from './pages/Messages.svelte';
  import Audit from './pages/Audit.svelte';
  import Settings from './pages/Settings.svelte';

  const pages = {
    keys: { title: 'Keys', c: Keys },
    permissions: { title: 'Permissions', c: Permissions },
    bots: { title: 'Bots', c: Bots },
    commands: { title: 'Commands', c: Commands },
    services: { title: 'Services', c: Services },
    messages: { title: 'Messages', c: Messages },
    audit: { title: 'Audit log', c: Audit },
    settings: { title: 'Settings', c: Settings },
  } as const;
  type Page = keyof typeof pages;

  let state = $state<'loading' | 'uninitialized' | 'sealed' | 'unsealed'>('loading');
  let loggedIn = $state(false);
  let page = $state<Page>(readHash());
  let loadError = $state('');

  function readHash(): Page {
    const h = location.hash.replace(/^#\/?/, '');
    return (h in pages ? h : 'keys') as Page;
  }

  async function refresh() {
    try {
      const s = await get('/state');
      state = s.state;
      loggedIn = s.logged_in;
      setCsrf(s.csrf);
      loadError = '';
    } catch (e: any) {
      loadError = e.message;
    }
  }

  async function logout() {
    try { await post('/logout'); } catch { /* already gone */ }
    setCsrf('');
    await refresh();
  }

  onUnauthorized(() => { refresh(); });
  $effect(() => {
    const on = () => (page = readHash());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  });
  refresh();

  const Current = $derived(pages[page].c);
</script>

{#if loadError}
  <div class="card narrow"><p class="error">{loadError}</p><button onclick={refresh}>Retry</button></div>
{:else if state === 'loading'}
  <p class="muted narrow">Loading…</p>
{:else if state === 'uninitialized'}
  <Setup done={refresh} />
{:else if state === 'sealed'}
  <Unlock done={refresh} />
{:else if !loggedIn}
  <Login done={refresh} />
{:else}
  <header>
    <strong>Gateway</strong>
    <nav>
      {#each Object.entries(pages) as [key, p]}
        <a href={'#/' + key} class:active={page === key}>{p.title}</a>
      {/each}
    </nav>
    <span class="spacer"></span>
    <button onclick={logout}>Log out</button>
  </header>
  <main><Current /></main>
{/if}
