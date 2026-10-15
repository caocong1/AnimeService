'use strict';
(() => {
  shell('subtitles');
  let openedTarget=false;
  const stages={waiting_review:'待逐句校订',waiting_alignment:'待核对日语与时间轴',waiting_drafts:'待收集基稿',accepted:'已验收'};
  async function load() {
    try {
      const d=await api('/subtitle-studio');
      $('#summary').textContent=`${d.summary.episodes} 集 · ${d.summary.bases} 份基稿档案 · ${d.summary.accepted} 个已验收观看版本`;
      $('#records').innerHTML=d.jobs.length ? d.jobs.map(j => `<details class="more" id="episode-${j.show_id}-${j.episode}"><summary>${esc(j.title)} · 第 ${j.episode} 集<span class="aside">${esc(stages[j.state] || '制作中')}</span></summary><div class="body"><p>第 ${j.payload.season} 季 · ${j.payload.base_count} 份基稿 · ${j.payload.chinese_texts} 套原生中文文本 · ${j.payload.japanese_drafts} 份日语基稿${j.payload.estimated_bases ? ` · ${j.payload.estimated_bases} 份待核实的 OCR / 估算稿` : ''}</p><p>相同文本保留各来源记录；基稿数量不代表独立译者数量。</p><p><a class="link" href="/show/${j.show_id}">查看作品</a>${j.payload.workbook ? ` · <a class="link" href="/api/subtitle-studio/${j.show_id}/${j.episode}/workbook">查看校订工作稿</a>` : ''}</p></div></details>`).join('') : '<p class="muted">完成样本后会建立该集的基稿档案。</p>';
      for (let i=0;i<d.jobs.length;i++) {
        const groups=d.jobs[i].payload.source_coverage || [];
        if (!groups.length) continue;
        const p=document.createElement('p');p.className='muted';
        p.textContent=groups.map(g=>`${g.publisher}：${g.available_this_episode ? '本集已发现' : '本集暂未见'}，已知最早第${g.first_seen_episode}集、最近第${g.last_seen_episode}集`).join('；');
        $('#records').children[i]?.querySelector('.body')?.append(p);
      }
      if (!openedTarget && /^#episode-\d+-\d+$/.test(location.hash)) {
        const target=document.getElementById(location.hash.slice(1));
        if(target) { target.open=true;target.style.scrollMarginTop='calc(var(--bar-h) + var(--s-5))';target.scrollIntoView();openedTarget=true; }
      }
    } catch(e) { $('#summary').textContent=e.message; }
  }
  load();setInterval(load,30000);
})();
