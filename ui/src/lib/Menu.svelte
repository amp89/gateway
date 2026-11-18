<script lang="ts">
  // Dropdown: a button that opens a list of actions. The list is position:fixed next to the button, so table
  // scroll boxes never clip it. It opens upward when there is no room below and follows the button when the
  // page or a table scrolls (closes once the button leaves the screen). Closes on pick, outside click, Esc, Tab.
  // Arrow keys move between items.
  import { tick } from 'svelte';
  type Item = { label: string; run: () => unknown } | { divider: true };
  let { label, items, title = '', done = false }: { label: string; items: Item[]; title?: string; done?: boolean } = $props();
  let open = $state(false), pos = $state<{ top: number; left: number } | null>(null);
  let btn: HTMLButtonElement, list: HTMLElement | undefined = $state();

  /** Put the list under (or above) the button, kept inside the window. */
  function place() {
    if (!open || !list) return;
    const r = btn.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) { hide(); return; }
    const w = list.offsetWidth, h = list.offsetHeight, gap = 4, edge = 8;
    const left = Math.min(Math.max(edge, r.right - w), innerWidth - w - edge);
    const top = r.bottom + gap + h <= innerHeight - edge ? r.bottom + gap : Math.max(edge, r.top - gap - h);
    pos = { top, left };
  }
  async function show() {
    pos = null;
    open = true;
    await tick();
    place();
    list?.querySelector<HTMLElement>('[role=menuitem]')?.focus({ preventScroll: true });
  }
  function hide(focusButton = false) { open = false; if (focusButton) btn?.focus({ preventScroll: true }); }
  function pick(it: Item) { if ('run' in it) { hide(true); it.run(); } }
  function keys(e: KeyboardEvent) {
    if (!open || !list) return;
    const all = [...list.querySelectorAll<HTMLElement>('[role=menuitem]')];
    const i = all.indexOf(document.activeElement as HTMLElement);
    if (e.key === 'Escape') { e.preventDefault(); hide(true); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); all[(i + 1) % all.length]?.focus({ preventScroll: true }); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); all[(i - 1 + all.length) % all.length]?.focus({ preventScroll: true }); }
    else if (e.key === 'Tab') hide();
  }
  function outside(e: MouseEvent) {
    const t = e.target as Node;
    if (open && !btn.contains(t) && !list?.contains(t)) hide();
  }
</script>

<svelte:window onclick={outside} onkeydown={keys} onresize={place} onscrollcapture={place} />
<button type="button" class="menu-btn" class:done bind:this={btn} {title} aria-haspopup="menu" aria-expanded={open}
  onclick={() => (open ? hide() : show())}>{label} <span aria-hidden="true">▾</span></button>
{#if open}
  <div class="menu-list" role="menu" bind:this={list} style:top="{pos?.top ?? 0}px" style:left="{pos?.left ?? 0}px"
    style:visibility={pos ? 'visible' : 'hidden'}>
    {#each items as it, i (i)}
      {#if 'divider' in it}<div class="menu-divider" role="separator"></div>
      {:else}<button type="button" role="menuitem" tabindex="-1" onclick={() => pick(it)}>{it.label}</button>{/if}
    {/each}
  </div>
{/if}
