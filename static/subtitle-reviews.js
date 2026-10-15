'use strict';
(() => {
  shell('library');
  const sid=Number(new URLSearchParams(location.search).get('show_id')) || 0;
  const query=sid ? `?show_id=${sid}` : '';
  $('#export').href='/api/source-review/subtitles/export'+query;
  if (sid) { $('#scope').href='/show/'+sid+'#source-review'; $('#scope').textContent='返回作品'; }
  else $('#overview').open=true;
  const time=s => `${Math.floor(s/60).toString().padStart(2,'0')}:${Math.floor(s%60).toString().padStart(2,'0')}`;
  const date=t => t ? new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) : '原评审时间未记录';
  async function load() {
    try {
      const result=await api('/source-review/subtitles/history'+(query || '?')+(query ? '&' : '')+'latest='+!$('#history').checked);
      $('#summary').textContent=`${result.records.length} 条${result.latest_only ? '当前' : '历史'}记录 · 共 ${result.history_versions} 个评审版本`;
      $('#note').textContent=result.note;
      $('#records').innerHTML=result.records.length ? result.records.map(r => `<details class="more"><summary>${esc(r.publisher)} · ${r.translation_score == null ? '未评分' : '字幕 '+r.translation_score}<span class="aside">${r.confidence === 'medium' ? '抽样' : r.confidence === 'high' ? '完整评测' : '资格核查'}</span>${icon('chevron')}</summary><div class="body">
        <p><a class="link" href="/show/${r.show_id}#source-review">${esc(r.show_title)}</a> · 第 ${esc(r.season ?? '?')} 季第 ${esc(r.episode)} 集<br>评分依据 ${esc(date(r.score_reviewed_at))}（北京时间） · 记录版本 ${r.record_id}</p>
        <p>${esc(r.release_title)}<br>视频片源声明：${esc(r.claimed_video_provider)} · ${r.release_delay_hours == null ? '发布时间待核实' : '首播后 '+r.release_delay_hours+' 小时发布'}</p>
        ${r.current_playback_exclusion ? `<p class="warn-line">${esc(r.current_playback_exclusion)}</p>` : ''}
        ${r.shared_text_with.length ? `<p><b>相同中文文本：</b>${r.shared_text_with.map(s=>esc(s.publisher)+'（候选 '+s.candidate_id+'）').join('、')}。统计独立译稿时需去重，不能据此认定译者归属。</p>` : ''}
        ${r.findings.length ? `<ul>${r.findings.map(f=>`<li><b>${time(f.seconds)} · ${f.severity === 'major' ? '重要差异' : f.severity === 'minor' ? '轻微差异' : '观察'}</b><br>${esc(f.finding)}<br><span class="muted">依据：${esc(f.basis)}</span></li>`).join('')}</ul>` : '<p class="muted">尚未拆分逐条差异，完整实评依据保留如下。</p>'}
        <p><b>实评记录</b><br>${esc(r.evidence)}</p>
        <p class="muted"><b>日语及原作依据</b><br>${esc(r.original_basis)}<br>评审者：${esc(r.reviewer)}</p>
        <p>${r.chinese_text_tracks.map(t=>`${esc(t.label || t.language)}：${t.cues == null ? '文本身份未核实' : t.cues+' 条文本'}`).join(' · ') || '硬字幕或无中文文本轨：不以OCR判断译稿身份'}</p>
      </div></details>`).join('') : '<p class="empty">还没有保存的字幕评审。</p>';
    } catch(e) { $('#records').textContent=e.message; }
  }
  $('#history').addEventListener('change',load);
  async function coverage() {
    try {
      const result=await api('/source-review/overview'),s=result.summary;
      $('#coverage-summary').textContent=`${s.enrolled} 部 · 已选 ${s.selected} · 比较中 ${s.sampling}`;
      const stages={selected:'已确定主源',waiting_samples:'等待样本下载/分析',waiting_review:'等待实物及日语核验',waiting_source:'等待可评样本',protected:'已暂停参与'};
      $('#coverage').innerHTML=result.shows.map(r=>`<p><a class="link" href="/show/${r.show_id}#source-review">${esc(r.title)}</a> · ${stages[r.stage] || '待核查'}<br><span class="muted">完成样本 ${r.ready_samples} · 已实评中文 ${r.reviewed_chinese_samples} · 待下载/分析 ${r.pending_samples}${r.errored_samples ? ' · 取样受阻 '+r.errored_samples : ''}</span>${r.primary ? '<br>主源：'+esc(r.primary) : ''}${r.backups.length ? '<br>替补：'+r.backups.map(esc).join('；') : ''}</p>`).join('') || '<p class="muted">尚无已核验并纳入选源的想看新番；加入个人列表并通过核验后自动比较。</p>';
    } catch(e) { $('#coverage-summary').textContent='暂时无法读取';$('#coverage').textContent=e.message; }
  }
  App.ready.then(()=>Promise.all([load(),coverage()]));
})();
