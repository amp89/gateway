<script lang="ts">
  // Native <dialog> popup. Mount it with {#if open}; Esc, ✕ or a click outside closes it and calls onclose.
  import type { Snippet } from 'svelte';
  let { title, onclose, children, wide = false }: { title: string; onclose: () => void; children: Snippet; wide?: boolean } = $props();
  let dlg: HTMLDialogElement;
  let downOnBackdrop = false;
  const uid = `dlg-${Math.random().toString(36).slice(2, 9)}`;
  $effect(() => {
    dlg.showModal();
    return () => { if (dlg.open) dlg.close(); };
  });
</script>

<dialog bind:this={dlg} class:wide aria-labelledby={uid} onclose={onclose}
  onmousedown={(e) => (downOnBackdrop = e.target === dlg)}
  onclick={(e) => { if (downOnBackdrop && e.target === dlg) dlg.close(); downOnBackdrop = false; }}>
  <div class="dlg-body">
    <div class="dlg-head"><h2 id={uid}>{title}</h2><button type="button" class="x" aria-label="Close dialog" title="Close (Esc)" onclick={() => dlg.close()}>✕</button></div>
    {@render children()}
  </div>
</dialog>
