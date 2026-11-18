<script lang="ts">
  import { get, del, when } from '../api';
  let bots = $state<any[]>([]), botId = $state<string>(''), msgs = $state<any[]>([]), next = $state<number | null>(null), error = $state('');

  async function load(more = false) {
    error = '';
    try {
      const q = new URLSearchParams();
      if (botId) q.set('bot_id', botId);
      if (more && next) q.set('before_id', String(next));
      const r = await get(`/messages?${q}`);
      msgs = more ? [...msgs, ...r.messages] : r.messages;
      next = r.next_before_id;
    } catch (e: any) { error = e.message; }
  }
  async function remove(m: any) { try { await del(`/messages/${m.id}`); msgs = msgs.filter((x) => x.id !== m.id); } catch (e: any) { error = e.message; } }
  const botName = (id: number) => bots.find((b) => b.id === id)?.name ?? id;
  const sender = (s: any) => s.username ? '@' + s.username : [s.first_name, s.last_name].filter(Boolean).join(' ') || s.id;
  get('/bots').then((r) => (bots = r.bots));
  load();
</script>

<div class="card">
  <h1>Messages</h1>
  <label><span>Bot</span><select bind:value={botId} onchange={() => load()}><option value="">All</option>{#each bots as b}<option value={String(b.id)}>{b.name}</option>{/each}</select></label>
  {#if error}<p class="error">{error}</p>{/if}
  <div class="scroll"><table>
    <thead><tr><th>Received</th><th>Bot</th><th>From</th><th>Text</th><th></th></tr></thead>
    <tbody>
      {#each msgs as m (m.id)}
        <tr><td>{when(m.received_at)}</td><td>{botName(m.bot_id)}</td><td>{sender(m.sender)}</td><td style="white-space:pre-wrap">{m.text}</td>
          <td><button class="danger" onclick={() => remove(m)}>Delete</button></td></tr>
      {:else}<tr><td colspan="5" class="muted">No messages.</td></tr>{/each}
    </tbody>
  </table></div>
  {#if next}<button onclick={() => load(true)}>Older</button>{/if}
</div>
