'use strict';
(function(root) {
  const editions=root.FinishedReleases || (typeof require==='function'?require('./finished-releases.js'):null);
  const steps=[['writing','整集文字校订'],['screen_text','画面字翻译'],['lyrics','OP / ED 歌词'],['placement','字幕位置避让'],['effects','必要动效'],['refining','语义与时轴细调'],['layout','排版与画面检查'],['rendering','整集视频输出'],['checking','解码与播放检查'],['publishing','上架交付']];
  function pending(j) {
    const all=Object.values(j.resources||{}).reduce((n,x)=>n+x.complete+x.downloading+x.pending+x.missing,0);
    if(['unknown','committing'].includes(j.dispatch_state))return {category:'making',stage:'starting',label:j.dispatch_state==='unknown'?'制作启动回执待确认':'正在启动制作',next:'核实唯一负责人的启动回执后继续，期间保留原任务，不重复开会话。'};
    if(j.dispatch_state==='blocked-stopped' || j.state==='blocked')return {category:j.dispatch_state==='blocked-stopped'?'making':'preparing',stage:'paused',label:'暂停待处理',next:'补齐本集缺件或解决实际输出问题后，由同一负责人继续。'};
    if(j.dispatch_state==='acknowledged' || j.state==='production') {
      const stage=j.progress?.stage,step=steps.find(s=>s[0]===stage);
      return {category:'making',stage:step?stage:'active',label:step?step[1]:'负责人已开始整集制作',next:step?({writing:'完成全片文字后，统一排版与检查画面。',layout:'修复阅读和遮挡问题后，输出完整视频。',rendering:'视频输出完成后，检查全片解码、原音轨和观看入口。',checking:'播放、跳转和下载通过后，登记可观看成品。',publishing:'v0上架后继续v1；v1完成并记录未决项后释放制作槽。'})[stage]||'完成本阶段实际检查和未决记录后继续v1，不跳过其它必做项。':'负责人连续完成整集；具体阶段尚未回报，不用旧工作稿估算进度。'};
    }
    if(j.ready || j.state==='ready' || j.kind==='late' && j.state==='late-review')return {category:'waiting',stage:'queued',label:j.kind==='late'?'修订资料待比对':'材料已齐 · 排队待开始',next:'制作槽空闲后，交给本集唯一会话连续完成；不需要逐集再确认。'};
    const counts=Object.values(j.resources||{}),downloading=counts.some(x=>x.downloading),pendingReceipt=counts.some(x=>x.pending),complete=counts.some(x=>x.complete);
    let label='待开始 · 等待素材',next='后台查找本集可用源；视频与中文字幕到齐后核对、排队，再连续完成首版。';
    if(downloading){label='素材下载中';next='下载完成后核对归属、盘点字幕并检查原片候选。';}
    else if(pendingReceipt){label='素材待提交 / 回执核查';next='后台核实下载任务回执；未知任务保留，确认后继续下载，不重复提交。';}
    else if(j.material_ready&&!j.clean_review_ready){label='素材已齐 · 待检查原片';next='检查本集候选画面和软字幕关闭情况，再准备输出并排队。';}
    else if(j.material_ready&&!j.capability_ready){label='素材已齐 · 准备输出通道';next='确认本机整集字幕输出与播放工具可用后，自动排队。';}
    else if(complete&&!j.chinese_bases){label='视频已到 · 待盘点字幕';next='从已完成视频盘点真实中文字幕，归档后核对本集，不重新下载。';}
    else if(complete){label='素材已到 · 待匹配本集';next='核视频归属、原稿与本集时轴匹配，检查后排队。';}
    else if(j.chinese_bases){label='字幕已收集 · 等待视频';next='取得匹配本集的完整视频后继续，已有原稿保留。';}
    return {category:all||j.chinese_bases?'preparing':'waiting',stage:'materials',label,next};
  }
  function gaps(j) {
    const reasons=j.reasons||[],items=[];
    if(reasons.includes('needs_verified_completed_media'))items.push('完整可用视频');
    if(reasons.includes('completed_media_needs_inventory_or_reconcile') || reasons.includes('needs_completed_manifest_and_ownership'))items.push('已下载视频的本集归属与匹配');
    if(reasons.includes('target_changed_or_missing'))items.push('重新匹配有效视频');
    if(reasons.includes('needs_reliable_chinese_dialogue_base'))items.push('可用中文字幕原稿');
    if(j.material_ready && !j.clean_review_ready)items.push('原片候选画面检查');
    if(j.material_ready && j.clean_review_ready && !j.capability_ready)items.push('本地整集输出准备');
    if(reasons.includes('archived_input_changed_or_invalid'))items.push('原稿文件核对');
    return [...new Set(items)];
  }
  function compose(archives,releases,view) {
    const records=new Map(),source=new Map((archives||[]).map(e=>[`${e.show_id}/${e.season}/${e.episode}`,e]));
    for(const r of releases||[]) {
      const key=`${r.show_id}/${r.season}/${r.episode}`,prior=records.get(key);if(prior?.release && editions.compare(prior.release,r)>=0)continue;
      records.set(key,{key,show_id:r.show_id,season:r.season,episode:r.episode,title:r.title,release:r,archive:source.get(key),category:'done',stage:'done',label:'已做好 · 可直接观看',next:'当前版本已交付；新材料或反馈只增量修订，观看版本继续保留。',edition:`v${r.version}`,gaps:[],revisions:[]});
    }
    for(const j of view?.jobs||[]) {
      const key=`${j.show_id}/${j.season}/${j.episode}`,existing=records.get(key);if(!j.scope_valid || j.watched && !existing)continue;
      if(['late','refinement'].includes(j.kind)) {if(existing && !j.watched && !['accepted','finished'].includes(j.state))existing.revisions.push({...j,...pending(j),edition:j.kind==='refinement'?'v1 精校细调':'修订版',gaps:gaps(j)});continue;}
      if(existing){existing.job=j;if(j.refinement_pending&&!j.watched)existing.revisions.push({...j,...pending(j),edition:'v1 精校细调',gaps:gaps(j)});continue;}
      if(j.delivery_finished || ['accepted','finished'].includes(j.state))continue;
      records.set(key,{...j,key,archive:source.get(key),...pending(j),edition:j.edition==='v1'?'v1 精校细调':'v0 观看版',gaps:gaps(j),revisions:[]});
    }
    for(const a of archives||[])if(a.build_id){const key=`${a.show_id}/${a.season}/${a.episode}`;if(!records.has(key))records.set(key,{...a,key,archive:a,category:'done',stage:'done',label:'已做好 · 可直接观看',next:'已交付版本继续可看。',edition:'已验收版',gaps:[],revisions:[]});}
    const rows=[...records.values()],order={making:0,done:1,preparing:2,waiting:3};for(const row of rows)row.release_versions=(releases||[]).filter(r=>`${r.show_id}/${r.season}/${r.episode}`===row.key).sort((a,b)=>editions.compare(b,a));rows.sort((a,b)=>order[a.category]-order[b.category] || (b.release?.finished_at||'').localeCompare(a.release?.finished_at||'') || a.show_id-b.show_id || a.episode-b.episode);
    return {rows,history:[...source.values()].filter(a=>!records.has(`${a.show_id}/${a.season}/${a.episode}`)),counts:{done:rows.filter(r=>r.category==='done').length,making:rows.filter(r=>r.category==='making'||r.revisions.some(v=>v.category==='making')).length,preparing:rows.filter(r=>r.category==='preparing').length,waiting:rows.filter(r=>r.category==='waiting'||r.revisions.some(v=>v.category==='waiting')).length}};
  }
  const api={steps,compose,gaps,pending};if(typeof module!=='undefined'&&module.exports)module.exports=api;else root.SubtitleDeliveryModel=api;
})(typeof window==='undefined'?globalThis:window);
