'use strict';
/* Reuses the existing management forms, tables and status components. */
async function renderBangumi() {
  const s = await api('/bangumi-sync'), opts = s.options;
  const table = (cls, head, rows) => `<div class="table-wrap"><table class="m-table ${cls}"><thead><tr>${head.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
  const labels = {pending: '等待同步', synced: '已同步', error: '自动重试中', blocked: '需要核验', conflict: '需要处理冲突', skipped: '未在个人列表'};
  $('#sec-bangumi').innerHTML = `
    <h2 class="m-sub" style="margin-top:0">把追番与观看记录同步到 Bangumi</h2>
    <p class="muted">本系统 → Bangumi。首次启用会同步个人列表，之后本地变更通常在数秒内进入同步；断网后自动补发。弹弹play 的已看记录需先经过本机每 60 秒的同步。</p>
    <p>想看 → 想看；试看 / 在追 → 在看；暂搁 → 搁置；弃番 → 抛弃；看完 → 看过。只有明确加入个人列表的作品参与同步，下载和播放位置不计为已看。</p>
    <div class="panel" style="margin-block:var(--s-4)">
      <p>${s.account ? `已连接 <b>${esc(s.account.nickname)}</b> · ${esc(s.account.username)}` : '尚未连接 Bangumi 账号'} · ${opts.enabled ? '自动同步已开启' : '自动同步未开启'}</p>
      <p class="muted">个人列表 ${s.scope_count} 部 · 已同步 ${s.counts.synced || 0} · 待同步 ${s.counts.pending || 0} · 待处理 ${(s.counts.blocked || 0) + (s.counts.conflict || 0) + (s.counts.error || 0)}${s.health.success ? ` · 最近成功 ${ago(s.health.success)}` : ''}</p>
      ${s.health.error ? `<p class="error-line" role="status">${esc(s.health.error)}</p>` : ''}
      <div class="links"><button class="btn" type="button" data-bgm-action="refresh">刷新状态</button>${App.local && s.account ? '<button class="btn" type="button" data-bgm-action="retry">重试失败项</button><button class="btn danger" type="button" data-bgm-action="disconnect">断开账号</button>' : ''}</div>
    </div>
    ${App.local ? `
      <form id="bangumi-connect" class="form-grid" style="max-width:640px">
        <label class="field wide"><span>Bangumi Access Token${s.account ? '（重新授权时填写）' : ''}</span><input name="access_token" type="password" autocomplete="off" required minlength="10" maxlength="4096" aria-describedby="bangumi-token-help"></label>
        <p id="bangumi-token-help" class="wide muted">在 <a href="https://next.bgm.tv/demo/access-token" target="_blank" rel="noopener noreferrer">Bangumi 官方令牌页面</a> 登录并生成令牌，仅粘贴到此本机页面。凭据使用 Windows 用户加密保存，页面不会回显；令牌过期后需重新连接。</p>
        <div class="wide"><button class="btn" type="submit">核验并连接账号</button></div>
      </form>
      <form id="bangumi-options" class="form-grid" style="max-width:640px;margin-top:var(--s-6)">
        <label class="field wide"><span><input name="private_new" type="checkbox" ${opts.private_new ? 'checked' : ''}> 新建收藏仅自己可见</span></label>
        <label class="field wide"><span><input name="ratings" type="checkbox" ${opts.ratings ? 'checked' : ''}> 同步本系统的个人评分</span></label>
        <label class="field wide"><span><input name="comments" type="checkbox" ${opts.comments ? 'checked' : ''}> 同步本系统的短评</span></label>
        <p class="wide muted">已有收藏的隐私和标签保持原值。勾选评分 / 短评后，以本地非空内容更新；本系统已经同步过的内容随后清空也会同步。弃番原因只留在本机。</p>
        <label class="field wide"><span><input name="enabled" type="checkbox" ${opts.enabled ? 'checked' : ''} ${s.account ? '' : 'disabled'}> 自动同步个人列表（首次以本地状态更新 Bangumi）</span></label>
        <div class="wide"><button class="btn primary" type="submit">保存同步设置</button></div>
      </form>` : '<p class="muted">请在家中电脑连接账号和修改同步设置。</p>'}
    <h3 class="m-sub">作品同步结果</h3>
    <p class="muted">分集按已核验的章节 ID 同步，无法唯一对应的记录会停留在“需要核验”。Bangumi 中另行修改过的记录会提示冲突，核对后可逐作品选择使用本地记录。</p>
    ${s.items.length ? table('', ['作品', '本地状态 / 已看', '同步结果'], s.items.map(x => `<tr><td><a href="/show/${x.show_id}">${esc(x.title)}</a></td><td>${STATES[x.local_state] || esc(x.local_state)} · ${x.watched} 集</td><td>${labels[x.state] || esc(x.state)}${x.error ? `<div class="warn-line">${esc(x.error)}</div>` : ''}${x.last_success ? `<div class="muted">${ago(x.last_success)}</div>` : ''}${App.local && x.state === 'conflict' ? `<details><summary>核对冲突</summary><p><a href="https://bgm.tv/subject/${x.show_id}" target="_blank" rel="noopener noreferrer">打开 Bangumi 作品</a></p><p>确认后将用本地状态及本系统管理的分集记录更新 Bangumi。</p><button class="btn" type="button" data-bgm-resolve="${x.show_id}">确认使用本地记录</button></details>` : ''}</td></tr>`)) : '<p class="muted">个人列表尚无作品。</p>'}`;
}

document.addEventListener('submit', e => {
  if (!['bangumi-connect', 'bangumi-options'].includes(e.target.id)) return;
  e.preventDefault();
  const f = e.target;
  act(f.querySelector('[type=submit]'), async () => {
    if (f.id === 'bangumi-connect') {
      const input = f.elements.access_token;
      const value = input.value;
      input.value = '';
      const result = await api('/bangumi-sync/connect', {access_token: value});
      toast(result.options.enabled ? '账号已核验，自动同步继续运行' : '账号已核验，请保存同步设置以启用');
    } else {
      const body = Object.fromEntries(['enabled', 'private_new', 'ratings', 'comments'].map(k => [k, f.elements[k].checked]));
      await api('/bangumi-sync/settings', body);
      toast(body.enabled ? '已启用自动同步' : '同步设置已保存');
    }
    await renderBangumi();
  });
});

document.addEventListener('click', e => {
  const b = e.target.closest('[data-bgm-action],[data-bgm-resolve]');
  if (!b) return;
  act(b, async () => {
    if (b.dataset.bgmResolve) await api('/bangumi-sync/retry', {show_id: Number(b.dataset.bgmResolve), resolve: true, confirm: '使用本地记录'});
    else if (b.dataset.bgmAction === 'retry') await api('/bangumi-sync/retry', {});
    else if (b.dataset.bgmAction === 'disconnect') await api('/bangumi-sync/disconnect', {});
    await renderBangumi();
  });
});
