'use strict';
/* 片库: every file on disk, and the watch history mirrored from the desktop player. */
(() => {
  shell('library');
  const p = new URLSearchParams(location.search);
  let tab = p.get('tab') === 'history' ? 'history' : 'files', offset = 0, total = 0, timer, seq = 0, pending = p.get('all') ? 0 : 1;
  const showFilter = Number(p.get('show')) || 0;
  $('#fq').value = p.get('q') || '';

  function setTab(t) {
    tab = t;
    $$('[data-tab]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.tab === t)));
    $('#files').hidden = t !== 'files';
    $('#history').hidden = t !== 'history';
    const u = new URLSearchParams(location.search);
    t === 'history' ? u.set('tab', 'history') : u.delete('tab');
    history.replaceState(null, '', '/library' + (u.size ? '?' + u : ''));
    t === 'files' ? loadFiles() : loadHistory();
  }

  async function loadFiles() {
    const id = ++seq, q = $('#fq').value.trim();
    try {
      const rows = await api('/web/media?limit=500&q=' + encodeURIComponent(q));
      if (id !== seq) return;
      $('#summary').textContent = rows.length >= 500 ? '前 500 个文件' : rows.length + ' 个文件';
      if (!rows.length) {
        $('#file-list').innerHTML = `<div class="empty"><p>${q ? `没有匹配「${esc(q)}」的文件` : '片库里还没有文件'}</p></div>`;
        return;
      }
      const folders = new Map();
      for (const r of rows) {
        const key = r.show_id ? 's' + r.show_id : 'f' + r.folder;
        if (!folders.has(key)) folders.set(key, { name: r.folder, show: r.show_id, items: [] });
        folders.get(key).items.push(r);
      }
      const open = q || folders.size <= 2;
      $('#file-list').innerHTML = [...folders.values()].map(f => {
        f.items.sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0) || a.name.localeCompare(b.name));
        const legacy = f.items.some(x => x.legacy && !x.show_id);
        return `<details class="folder"${open ? ' open' : ''}><summary>${esc(f.name)}<span class="count">${f.items.length}</span>${legacy ? '<span class="chip">旧片库</span>' : ''}${icon('chevron')}</summary>
          ${f.show ? `<a class="btn small quiet" href="/show/${f.show}">作品页</a>` : ''}
          ${f.items.map(x => `<a class="file-row" href="/watch?media=${x.id}"><span class="fname"><b>${x.show_id ? `第 ${x.episode} 集` : esc(x.name)}</b>${x.show_id ? `<span>${esc(x.name)}</span>` : ''}</span>
            <span class="size">${gib(x.size)}</span><span>${x.unverified ? '<span class="chip warn">待核验</span>' : ''}${icon('play')}</span></a>`).join('')}</details>`;
      }).join('');
    } catch (e) {
      $('#file-list').innerHTML = `<div class="empty"><h2>读取失败</h2><p>${esc(e.message)}</p></div>`;
    }
  }

  async function loadHistory() {
    const id = ++seq;
    try {
      $$('[data-pending]').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.pending) === pending)));
      const d = await api(`/history/dandan?offset=${offset}&show_id=${showFilter}&pending=${pending}&q=${encodeURIComponent($('#hq').value.trim())}`);
      if (id !== seq) return;
      total = d.total;
      const s = d.status;
      $('#summary').textContent = total + ' 条记录';
      $('#sync-status').innerHTML = !s ? '尚未同步' : s.ok
        ? `同步正常 · ${ago(s.success)} · 已同步 ${s.synced_watched || 0} 集`
        : `<span class="error-line">同步失败 · ${esc(s.error || '')}</span>`;
      $('#records').innerHTML = d.items.map(r => {
        const can = r.show_id && !r.finished;
        const when = Math.max(r.last_played || 0, r.source_watched || 0);
        return `<tr><td><label class="hit"><input type="checkbox" data-id="${esc(r.media_id)}" aria-label="选择 ${esc(r.title)}"${can ? '' : ' disabled'}></label></td>
          <td>${r.show_id ? `<a href="/show/${r.show_id}"><b>${esc(r.title)}</b> 第 ${r.episode} 集</a>` : `<b>${esc(r.title)}</b>`}<div class="muted" style="font-size:var(--text-xs);overflow-wrap:anywhere">${esc(r.name)}</div></td>
          <td class="nowrap">${ago(when)}${r.position ? `<div class="muted num">${clock(r.position)} / ${clock(r.duration)}</div>` : ''}</td>
          <td class="nowrap">${r.finished ? `<span class="seen">已看</span><div class="muted">${r.method === 'manual' ? '手动' : '同步'}</div>` : r.show_id ? '未看' : '<span class="muted">未关联作品</span>'}</td>
          <td><a class="btn small quiet" href="/watch?media=${esc(r.media_id)}" aria-label="播放">${icon('play')}</a></td></tr>`;
      }).join('') || `<tr><td colspan="5"><div class="empty"><p>${pending ? '没有待确认的记录' : '没有记录'}</p></div></td></tr>`;
      $('#page').textContent = total ? `${offset + 1}–${Math.min(offset + 100, total)} / ${total}` : '';
      $('#prev').disabled = offset === 0;
      $('#next').disabled = offset + 100 >= total;
      $('#prev').parentElement.hidden = total <= 100;
      $('#all').checked = false;
      updateConfirm();
    } catch (e) {
      $('#records').innerHTML = `<tr><td colspan="5"><div class="empty"><h2>读取失败</h2><p>${esc(e.message)}</p></div></td></tr>`;
    }
  }

  function updateConfirm() {
    const n = $$('[data-id]:checked').length;
    $('#confirm').disabled = !n;
    $('#confirm').textContent = n ? `确认看完 ${n} 集` : '确认看完';
  }

  document.addEventListener('click', e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.dataset.tab) return setTab(b.dataset.tab);
    if (b.dataset.pending) { pending = Number(b.dataset.pending); offset = 0; return loadHistory(); }
    if (b.id === 'sync') return act(b, async () => { const r = await api('/history/dandan/sync', {}); toast(r.ok ? '已同步' : r.error || '同步正在进行'); await loadHistory(); });
    if (b.id === 'confirm') return act(b, async () => {
      const ids = $$('[data-id]:checked').map(x => x.dataset.id);
      const r = await api('/history/dandan/confirm', { media_ids: ids });
      toast(`已确认 ${r.confirmed} 集看完`);
      await loadHistory();
    });
    if (b.id === 'prev') { offset = Math.max(0, offset - 100); loadHistory(); }
    if (b.id === 'next') { offset += 100; loadHistory(); }
  });
  document.addEventListener('change', e => {
    if (e.target.id === 'all') $$('[data-id]:not(:disabled)').forEach(x => { x.checked = e.target.checked; });
    if (e.target.matches('[data-id], #all')) updateConfirm();
  });
  $('#fq').addEventListener('input', e => { if (e.isComposing) return; clearTimeout(timer); timer = setTimeout(loadFiles, 250); });
  $('#hq').addEventListener('input', e => { if (e.isComposing) return; clearTimeout(timer); timer = setTimeout(() => { offset = 0; loadHistory(); }, 250); });

  boot().then(() => setTab(tab)).catch(e => toast(e.message));
})();
