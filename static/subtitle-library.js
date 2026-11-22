'use strict';
(() => {
 shell('subtitles');
 let model={rows:[],history:[],counts:{}},selected='all',loading=false,loadRevision=0,archiveEntries=[],releaseEntries=[],progressData=null;
 let detailedArchive=false;
 const chosen=new URLSearchParams(location.search).get('show')||'',labels={all:'全部',done:'可以观看',making:'制作中',preparing:'准备中',waiting:'待开始'};
 const duration=r=>{const n=Math.round(r.duration_seconds);return `${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;};
 function row(j){
  const r=j.release;
  return `<article class="subtitle-entry" data-delivery="${j.category}" data-key="${esc(j.key)}"><div class="subtitle-entry-head"><div><h2><a href="/show/${j.show_id}">${esc(j.title)}</a></h2><p>第 ${j.season} 季 · 第 ${j.episode} 集</p></div><span class="subtitle-status" data-ready="${Boolean(r||j.build_id)}">${j.category==='done'?'':esc(j.edition)+' · '}${esc(j.label)}</span></div>${r?`<p class="release-meta">${r.height?`${esc(r.height)}p`:'原画幅'} · ${duration(r)} · 日语原声 · 画面已带字幕 · ${esc(FinishedReleases.label(r))}</p><div class="subtitle-entry-actions"><a class="btn" href="${esc(r.player_url)}">播放成品</a><a class="link" href="${esc(r.video_url)}" download>下载视频</a><a class="link" href="${esc(r.subtitles_url)}" download>字幕和字体</a><a class="link" href="${esc(r.notes_url)}">版本说明</a></div>${(j.release_versions||[]).length>1?`<div class="subtitle-entry-actions" aria-label="选择观看版本">${j.release_versions.map(v=>`<a class="link" href="${esc(v.player_url)}">播放 ${esc(FinishedReleases.label(v))}</a>`).join('')}</div>`:''}`:j.build_id?`<div class="subtitle-entry-actions"><a class="btn" href="/watch?media=${esc(j.media_id)}">播放本集</a></div>`:j.next?`<p class="muted">${esc(j.next)}</p>`:''}</article>`;
 }
 function render(){
  const sid=$('#subtitle-show').value,q=$('#subtitle-search').value.trim().toLocaleLowerCase();
  const rows=model.rows.filter(j=>(!sid||String(j.show_id)===sid)&&j.title.toLocaleLowerCase().includes(q)&&(selected==='all'||j.category===selected));
  for(const b of $$('#delivery-filters button')){const k=b.dataset.filter;b.setAttribute('aria-pressed',String(k===selected));b.innerHTML=`${labels[k]} <span>${k==='all'?model.rows.length:model.counts[k]||0}</span>`;}
  $('#summary').textContent=`${model.counts.done||0} 集已做好 · ${model.counts.making||0} 集制作中 · ${model.counts.preparing||0} 集准备中 · ${model.counts.waiting||0} 集等待`;
  $('#result-count').textContent=`显示 ${rows.length} 集`;
  const empty={done:'还没有可以观看的字幕版。',making:'当前没有正在制作的集数。',preparing:'当前没有正在准备的集数。',waiting:'当前没有等待中的集数。',all:'没有符合条件的集数。'};
  $('#subtitle-records').innerHTML=rows.map(row).join('')||`<p class="empty">${empty[selected]}</p>`;
  $('#history-count').textContent=`历史材料档案（${model.history.length} 集）`;
  $('#subtitle-history').innerHTML=model.history.filter(a=>(!sid||String(a.show_id)===sid)&&a.title.toLocaleLowerCase().includes(q)).map(a=>`<li><a class="link" href="/show/${a.show_id}">${esc(a.title)}</a> · 第${a.episode}集 · ${a.base_count}份基稿${a.progress?` · 旧工作稿${a.progress.reviewed}/${a.progress.total}`:''}</li>`).join('')||'<li>没有符合筛选条件的历史档案。</li>';
 }
 async function projection(){
  const c=new AbortController(),t=setTimeout(()=>c.abort(),8000);
  try{
   const options={cache:'no-store',credentials:'same-origin',signal:c.signal};
   let r=await fetch('/api/subtitle-progress',options),live=true;
   if(r.status===404){r=await fetch('/static/subtitle-delivery-status.json',options);live=false;}
   if(!r.ok)throw Error('制作进度暂不可读取');
   const data=await r.json();
   if(data?.schema!==1||!Array.isArray(data.jobs)||!Number.isFinite(data.generated_at))throw Error('制作进度格式无效');
   return {...data,live:live&&data.live===true};
  }finally{clearTimeout(t);}
 }
 async function load(){
  if(loading)return;loading=true;const revision=++loadRevision;
  try{
   let archive={status:'pending'},releases={status:'pending'},view={status:'pending'};
   function present(){
   if(revision!==loadRevision)return;
   const results=[archive,releases,view],available=results.some(r=>r.status==='fulfilled');
   if(!available&&!model.rows.length){
    if(results.every(r=>r.status==='rejected')){$('#delivery-notice').hidden=false;$('#delivery-notice').textContent='字幕与成品暂时无法读取，请重试。';$('#subtitle-retry').hidden=false;}
    return;
   }
   const data=progressData;
   model=SubtitleDeliveryModel.compose(archiveEntries,releaseEntries,data);
   const prev=$('#subtitle-show').value||chosen,works=[...new Map(model.rows.map(j=>[j.show_id,j.title]))].sort((a,b)=>a[1].localeCompare(b[1],'zh-CN'));
   $('#subtitle-show').innerHTML='<option value="">全部作品</option>'+works.map(([sid,title])=>`<option value="${sid}">${esc(title)}</option>`).join('');if(works.some(([sid])=>String(sid)===prev))$('#subtitle-show').value=prev;
   const notices=[];if(view.status==='rejected')notices.push('制作进度暂不可读取，保留最近一次显示，请稍后刷新。');else if(data&&(!data.live||Date.now()/1000-data.generated_at>1800))notices.push('当前为历史准备快照，请以制作队列的实时状态为准。');if(releases.status==='rejected')notices.push('成品清单暂不可读取，保留最近一次显示，请稍后刷新。');if(archive.status==='rejected')notices.push('历史材料暂不可读取，请稍后重试。当前成品与制作进度仍可查看。');
   $('#delivery-notice').textContent=notices.join(' ');$('#delivery-notice').hidden=!notices.length;
   $('#updated-at').textContent=data?`${data.live?'实时制作状态':'准备快照'}更新于 ${new Date(data.generated_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}`:view.status==='pending'?'正在读取制作进度…':'制作进度暂不可用';
   $('#subtitle-retry').hidden=true;render();
   if(archive.status==='pending'){$('#history-count').textContent+=' · 读取中';if(!model.history.length)$('#subtitle-history').innerHTML='<li>正在读取历史材料…</li>';}
   }
   const observe=(promise,accept)=>Promise.resolve(promise).then(value=>{if(revision===loadRevision){accept({status:'fulfilled',value});present();}},reason=>{if(revision===loadRevision){accept({status:'rejected',reason});present();}});
   // Each source can render independently; slow diagnostics and folded history
   // never hold verified play buttons behind their timeouts.
   observe(api('/subtitle-library' + (detailedArchive?'':'?compact=true')),result=>{archive=result;if(result.status==='fulfilled')archiveEntries=result.value.entries;}).catch(e=>{if(revision===loadRevision){$('#delivery-notice').hidden=false;$('#delivery-notice').textContent=e.message;}});
   await Promise.all([
    observe(FinishedReleases.load({verifyMedia:true}),result=>{releases=result;if(result.status==='fulfilled')releaseEntries=result.value;}),
    observe(projection(),result=>{view=result;if(result.status==='fulfilled')progressData=result.value;})
   ]);
  }catch(e){if(revision===loadRevision){$('#delivery-notice').hidden=false;$('#delivery-notice').textContent=e.message;$('#subtitle-retry').hidden=false;}}finally{if(revision===loadRevision)loading=false;}
 }
 $('#filters').addEventListener('submit',e=>e.preventDefault());
 document.addEventListener('toggle',event=>{
   if(detailedArchive || !event.target.open || !event.target.matches?.('.history-archive,.subtitle-details'))return;
   detailedArchive=true;
   // Opening a record during the first read must still start the full read.
   loading=false;
   load();
 },true);
 $('#delivery-filters').addEventListener('click',e=>{const b=e.target.closest('button[data-filter]');if(b){selected=b.dataset.filter;render();}});
 $('#subtitle-show').addEventListener('change',()=>{const u=new URL(location.href);u.searchParams.delete('show');if($('#subtitle-show').value)u.searchParams.set('show',$('#subtitle-show').value);history.replaceState(null,'',u);render();});
 $('#subtitle-search').addEventListener('input',render);$('#subtitle-retry').addEventListener('click',load);$('#delivery-refresh').addEventListener('click',load);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});window.addEventListener('focus',()=>{if(!document.hidden)load();});setInterval(()=>{if(!document.hidden)load();},60000);load();
})();
