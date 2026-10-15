'use strict';
(() => {
 shell('subtitles');
 let model={rows:[],history:[],counts:{}},selected='all',loading=false;
 const chosen=new URLSearchParams(location.search).get('show')||'',labels={all:'全部',done:'已做好',making:'制作中',preparing:'准备中',waiting:'待开始'};
 const duration=r=>{const n=Math.round(r.duration_seconds);return `${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;};
 function materials(j){
  if(!j?.resources)return '';
  const names={original:'原片候选 · 生肉 / 可关软字幕',reference:'熟肉参考',unknown:'类型待核'};
  return `<div class="material-grid">${Object.entries(j.resources).filter(([k,x])=>k!=='unknown'||Object.values(x).some(Boolean)).map(([k,x])=>`<div><span>${names[k]}</span><strong>已完成 ${x.complete} 份</strong><small>下载中 ${x.downloading} · 待提交 / 核查 ${x.pending}${x.missing?` · 文件待核 ${x.missing}`:''}</small></div>`).join('')}<div><span>中文字幕原稿</span><strong>${j.chinese_bases||0} 份已归档</strong><small>简繁 / 同文不算独立译者</small></div></div>`;
 }
 function process(j){
  const n=SubtitleDeliveryModel.steps.findIndex(s=>s[0]===j.stage);
  return `<ol class="production-steps" aria-label="制作流程">${SubtitleDeliveryModel.steps.map(([k,label],i)=>`<li ${i===n?'aria-current="step"':''} data-phase="${i<n?'previous':i===n?'current':'future'}">${label}</li>`).join('')}</ol>${j.progress?.updated?`<p class="muted">${j.progress.total?`本阶段实际记录 ${j.progress.done} / ${j.progress.total} 条 · `:''}最近回报 ${ago(j.progress.updated)}</p>`:''}`;
 }
 function ongoing(j,revision=false){return `<div class="delivery-process${revision?' revision-process':''}">${revision?`<p class="stage-line"><strong>${esc(j.label)}</strong><span>${esc(j.edition||"修订版")}</span></p>`:''}${j.category==='making'?process(j):''}${j.gaps.length?`<p class="needed"><b>还缺：</b>${j.gaps.map(esc).join('、')}</p>`:''}<p class="next-action"><b>下一步：</b>${esc(j.next)}</p>${materials(j)}</div>`;}
 function row(j){
  const r=j.release,a=j.archive;
  return `<article class="subtitle-entry" data-delivery="${j.category}" data-key="${esc(j.key)}"><div class="subtitle-entry-head"><div><h2><a href="/show/${j.show_id}">${esc(j.title)}</a></h2><p>第 ${j.season} 季 · 第 ${j.episode} 集</p></div><span class="subtitle-status" data-ready="${Boolean(r||j.build_id)}">${j.category==='done'?'':esc(j.edition)+' · '}${esc(j.label)}</span></div>${r?`<p class="release-meta">${r.height?`${esc(r.height)}p`:'原画幅'} · ${duration(r)} · 日语原声 · 画面已带字幕 · ${esc(FinishedReleases.label(r))}</p><div class="subtitle-entry-actions"><a class="btn" href="${esc(r.player_url)}">播放成品</a><a class="link" href="${esc(r.video_url)}" download>下载视频</a><a class="link" href="${esc(r.subtitles_url)}" download>字幕和字体</a><a class="link" href="${esc(r.notes_url)}">版本说明</a></div><p class="muted">${r.edition==='v0'?'v0可立即观看，随后继续制作v1精校细调版。':'本版可直接观看，具体处理范围与未决项见版本说明。'}</p>${(j.release_versions||[]).length>1?`<div class="subtitle-entry-actions" aria-label="选择观看版本">${j.release_versions.map(v=>`<a class="link" href="${esc(v.player_url)}">播放 ${esc(FinishedReleases.label(v))}</a>`).join('')}</div>`:''}`:j.build_id?`<div class="subtitle-entry-actions"><a class="btn" href="/watch?media=${esc(j.media_id)}">播放本集</a></div>`:ongoing(j)}${j.revisions.map(v=>`<h3 class="revision-heading">后续版本 · 当前成品仍可看</h3>${ongoing(v,true)}`).join('')}<details class="subtitle-details"><summary>素材与制作记录</summary>${r?materials(j.job):''}<p>${a?`${a.base_count} 份历史基稿档案`:'首版完成后保留原稿和制作记录。'}</p>${a?.progress?`<p class="muted">旧工作稿书面记录 ${a.progress.reviewed} / ${a.progress.total} 条，独立于本版交付进度。</p>`:''}${a?.video_file?`<p class="muted">原视频：${esc(a.video_file)}</p>`:''}<a class="link" href="/subtitle-studio#episode-${j.show_id}-${j.episode}">打开制作档案</a></details></article>`;
 }
 function render(){
  const sid=$('#subtitle-show').value,q=$('#subtitle-search').value.trim().toLocaleLowerCase();
  const rows=model.rows.filter(j=>(!sid||String(j.show_id)===sid)&&j.title.toLocaleLowerCase().includes(q)&&(selected==='all'||j.category===selected||j.revisions.some(v=>v.category===selected)));
  for(const b of $$('#delivery-filters button')){const k=b.dataset.filter;b.setAttribute('aria-pressed',String(k===selected));b.innerHTML=`${labels[k]} <span>${k==='all'?model.rows.length:model.counts[k]||0}</span>`;}
  $('#summary').textContent=`${model.counts.done||0} 集已做好 · ${model.counts.making||0} 集制作中 · ${model.counts.preparing||0} 集准备中 · ${model.counts.waiting||0} 集待开始`;
  $('#result-count').textContent=`显示 ${rows.length} 集`;
  const empty={done:'还没有已交付成品。完整首版通过播放检查后会出现在这里。',making:'当前没有正在制作的集数。材料就绪后会由独立会话一次完成整集。',preparing:'当前没有正在准备素材的集数。',waiting:'当前没有待开始的集数。',all:'没有符合条件的交付记录。'};
  const open=new Set($$('#subtitle-records details[open]').map(d=>d.closest('article').dataset.key));
  $('#subtitle-records').innerHTML=rows.map(row).join('')||`<p class="empty">${empty[selected]}</p>`;
  for(const d of $$('#subtitle-records details'))if(open.has(d.closest('article').dataset.key))d.open=true;
  $('#history-count').textContent=`历史材料档案（${model.history.length} 集）`;
  $('#subtitle-history').innerHTML=model.history.filter(a=>(!sid||String(a.show_id)===sid)&&a.title.toLocaleLowerCase().includes(q)).map(a=>`<li><a class="link" href="/show/${a.show_id}">${esc(a.title)}</a> · 第${a.episode}集 · ${a.base_count}份基稿${a.progress?` · 旧工作稿${a.progress.reviewed}/${a.progress.total}`:''}</li>`).join('')||'<li>没有符合筛选条件的历史档案。</li>';
 }
 async function projection(){const c=new AbortController(),t=setTimeout(()=>c.abort(),8000);try{const r=await fetch('/static/subtitle-delivery-status.json',{cache:'no-store',signal:c.signal});if(!r.ok)throw Error('制作进度暂不可读取');return await r.json();}finally{clearTimeout(t);}}
 async function load(){
  if(loading)return;loading=true;
  try{
   const [archive,releases,view]=await Promise.allSettled([api('/subtitle-library'),FinishedReleases.load(),projection()]);
   if(archive.status==='rejected'&&releases.status==='rejected')throw Error('字幕与成品暂时无法读取，请重试。');
   const data=view.status==='fulfilled'&&view.value.schema===1?view.value:null;
   model=SubtitleDeliveryModel.compose(archive.status==='fulfilled'?archive.value.entries:[],releases.status==='fulfilled'?releases.value:[],data);
   const prev=$('#subtitle-show').value||chosen,works=[...new Map(model.rows.map(j=>[j.show_id,j.title]))].sort((a,b)=>a[1].localeCompare(b[1],'zh-CN'));
   $('#subtitle-show').innerHTML='<option value="">全部作品</option>'+works.map(([sid,title])=>`<option value="${sid}">${esc(title)}</option>`).join('');if(works.some(([sid])=>String(sid)===prev))$('#subtitle-show').value=prev;
   const notices=[];if(!data)notices.push('制作进度暂不可读取，待制作列表未显示。已交付成品仍可播放。');else if(Date.now()/1000-data.generated_at>1800)notices.push('准备信息尚未更新，以下显示最近一次记录。');if(releases.status==='rejected')notices.push('成品清单暂不可读取，请稍后刷新。');
   $('#delivery-notice').textContent=notices.join(' ');$('#delivery-notice').hidden=!notices.length;
   $('#updated-at').textContent=data?`准备记录更新于 ${new Date(data.generated_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})} · 制作阶段由负责人回报`:'制作进度暂不可用';
   $('#subtitle-retry').hidden=true;render();
  }catch(e){$('#delivery-notice').hidden=false;$('#delivery-notice').textContent=e.message;$('#subtitle-retry').hidden=false;}finally{loading=false;}
 }
 $('#filters').addEventListener('submit',e=>e.preventDefault());
 $('#delivery-filters').addEventListener('click',e=>{const b=e.target.closest('button[data-filter]');if(b){selected=b.dataset.filter;render();}});
 $('#subtitle-show').addEventListener('change',()=>{const u=new URL(location.href);u.searchParams.delete('show');if($('#subtitle-show').value)u.searchParams.set('show',$('#subtitle-show').value);history.replaceState(null,'',u);render();});
 $('#subtitle-search').addEventListener('input',render);$('#subtitle-retry').addEventListener('click',load);$('#delivery-refresh').addEventListener('click',load);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});setInterval(()=>{if(!document.hidden)load();},60000);load();
})();
