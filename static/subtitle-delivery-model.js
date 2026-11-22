'use strict';
(function(root) {
  const editions=root.FinishedReleases || (typeof require==='function'?require('./finished-releases.js'):null);
  const queue=root.SubtitleQueueModel || (typeof require==='function'?require('./subtitle-queue-model.js'):null);
  const steps=[['writing','整集文字校订'],['screen_text','画面字翻译'],['lyrics','OP / ED 歌词'],['placement','字幕位置避让'],['effects','必要动效'],['refining','语义与时轴细调'],['layout','排版与画面检查'],['rendering','整集视频输出'],['checking','解码与播放检查'],['publishing','上架交付']];
  function pending(j) {
    const counts = Object.values(j.resources || {});
    let status = 'waiting_video';
    if (j.dispatch_state === 'acknowledged') status = 'producing';
    else if (['unknown','committing','blocked-stopped'].includes(j.dispatch_state) || j.state === 'blocked') status = 'blocked';
    else if (j.ready || j.state === 'ready') status = 'queued';
    else if (counts.some(r => r.downloading)) status = 'downloading';
    else if (j.material_ready || counts.some(r => r.complete)) status = 'preparing';
    const work = j.work || {status, stage:j.progress?.stage};
    const group = queue.section(work), message = queue.describe(work);
    return {category:group === 'active' ? 'making' : group === 'preparing' ? 'preparing' : 'waiting',
      stage:work.stage || group, label:message.label, next:message.detail};
  }
  function compose(archives,releases,view) {
    const records=new Map(),source=new Map((archives||[]).map(e=>[`${e.show_id}/${e.season}/${e.episode}`,e]));
    for(const r of releases||[]) {
      const key=`${r.show_id}/${r.season}/${r.episode}`,prior=records.get(key);if(prior?.release && editions.compare(prior.release,r)>=0)continue;
      records.set(key,{key,show_id:r.show_id,season:r.season,episode:r.episode,title:r.title,release:r,archive:source.get(key),category:'done',stage:'done',label:'可以观看',next:'当前版本已交付；新材料或反馈只增量修订，观看版本继续保留。',edition:`v${r.version}`,gaps:[],revisions:[]});
    }
    for(const j of view?.jobs||[]) {
      const key=`${j.show_id}/${j.season}/${j.episode}`,existing=records.get(key);if(!j.scope_valid&&j.work?.status!=='unreleased' || j.watched && !existing)continue;
      if(['late','refinement'].includes(j.kind)) {if(existing && !j.watched && !['accepted','finished'].includes(j.state))existing.revisions.push({...j,...pending(j),edition:j.kind==='refinement'?'v1 精校细调':'修订版'});continue;}
      if(existing){existing.job=j;if(j.refinement_pending&&!j.watched)existing.revisions.push({...j,...pending(j),edition:'v1 精校细调'});continue;}
      if(j.delivery_finished || ['accepted','finished'].includes(j.state))continue;
      records.set(key,{...j,key,archive:source.get(key),...pending(j),edition:j.edition==='v1'?'v1 精校细调':'v0 观看版',revisions:[]});
    }
    for(const a of archives||[])if(a.build_id){const key=`${a.show_id}/${a.season}/${a.episode}`;if(!records.has(key))records.set(key,{...a,key,archive:a,category:'done',stage:'done',label:'可以观看',next:'已交付版本继续可看。',edition:'已验收版',gaps:[],revisions:[]});}
    const rows=[...records.values()],order={done:0,making:1,preparing:3,waiting:4};for(const row of rows)row.release_versions=(releases||[]).filter(r=>`${r.show_id}/${r.season}/${r.episode}`===row.key).sort((a,b)=>editions.compare(b,a));rows.sort((a,b)=>order[a.category]-order[b.category] || (b.release?.finished_at||'').localeCompare(a.release?.finished_at||'') || a.show_id-b.show_id || a.episode-b.episode);
    const counts=Object.fromEntries(['done','making','preparing','waiting'].map(k=>[k,rows.filter(r=>r.category===k).length]));
    return {rows,history:[...source.values()].filter(a=>!records.has(`${a.show_id}/${a.season}/${a.episode}`)),counts};
  }
  const api={steps,compose,pending};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.SubtitleDeliveryModel=api;
})(typeof window==='undefined'?globalThis:window);
