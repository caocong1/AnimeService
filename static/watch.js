'use strict';
const $=s=>document.querySelector(s),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token='',art=null,media=null,selected=[],comments=[],searchTimer,lastPosition=0,lastSave=0,activeShow=null;
let generation=0,danmuRevision=0,subtitleRevision=0,subtitleTracks=[],subtitleQueue=Promise.resolve();
async function api(path,body){const r=await fetch('/api/web'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Anime-Token':token},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error||d.detail||'请求失败');return d}
async function list(){const show=new URLSearchParams(location.search).get('show');const rows=await api('/media?limit=500&show_id='+(Number(show)||0)+'&q='+encodeURIComponent($('#media-search').value));$('#media-list').innerHTML=rows.map(x=>`<button class="media-item" data-media="${x.id}">${x.show_id?`${esc(x.folder)} · 第 ${x.episode} 集`:esc(x.name)}<small>${x.show_id?esc(x.name):esc(x.folder)} · ${(x.size/1024**3).toFixed(2)} GiB · ${x.unverified?'旧文件，完成记录待核对':x.legacy?'旧文件，只读':'项目下载'}</small></button>`).join('')||'<p>没有匹配的本机文件。</p>'}
async function saveProgress(){if(!art||!media||!Number.isFinite(art.duration)||art.duration<=0)return;const delta=art.currentTime-lastPosition;const playing=!art.paused&&delta>0&&delta<35;lastPosition=art.currentTime;await api('/media/'+media.id+'/progress',{position:art.currentTime,duration:art.duration,playing})}
function current(g,player){return generation===g&&art===player&&!player.isDestroy}
async function open(id){
 const g=++generation;++danmuRevision;++subtitleRevision;
 await saveProgress().catch(()=>{});if(g!==generation)return;
 if(art){art.destroy();art=null}media=null;
 $('#screen').innerHTML='<div class="screen-placeholder">正在打开视频…</div>';
 selected=[];comments=[];activeShow=null;subtitleTracks=[];renderSelected();
 $('#danmu-results').innerHTML='';$('#danmu-episodes').innerHTML='';$('#offset').value='0';
 $('#subtitle-choice').innerHTML='<option value="">关闭</option>';$('#subtitle-choice').disabled=true;
 $('#subtitle-note').textContent='正在读取字幕轨道…';$('#danmu-note').textContent='正在自动查找本集弹幕…';
 const item=await api('/media/'+id);if(g!==generation)return;media=item;lastPosition=0;lastSave=Date.now();
 document.querySelectorAll('[data-media]').forEach(b=>b.classList.toggle('active',b.dataset.media===id));
 $('#screen').innerHTML='';$('#playing-title').textContent=media.show_id?`${media.folder} · 第 ${media.episode} 集`:media.name;$('#desktop').disabled=false;$('#retry-danmu').disabled=false;
 $('#resume').disabled=!(media.progress?.position>0);$('#resume').textContent=media.progress?.position>0?'继续上次进度（'+Math.floor(media.progress.position/60)+':'+String(Math.floor(media.progress.position%60)).padStart(2,'0')+'）':'继续上次进度';
 $('#play-note').textContent='支持拖动进度、全屏与倍速。浏览器无法解码的视频仍可用弹弹play打开。';
 const player=art=new Artplayer({container:'#screen',url:'/api/web/media/'+id+'/stream',type:media.name.toLowerCase().endsWith('.mp4')?'mp4':'mkv',autoplay:false,volume:.6,theme:getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),lang:'zh-cn',setting:true,playbackRate:true,fullscreen:true,fullscreenWeb:true,subtitle:{escape:true,style:{color:'#fff',fontSize:'clamp(16px, 2.2vw, 28px)',textShadow:'0 1px 3px #000, 1px 0 2px #000'}},plugins:[artplayerPluginDanmuku({danmuku:[],emitter:false,fontSize:22,opacity:.85,antiOverlap:true,margin:[10,'25%'],beforeEmit:()=>false})]});
 player.on('video:loadedmetadata',()=>{if(current(g,player)&&new URLSearchParams(location.search).get('resume')==='1'&&item.progress?.position>0){player.currentTime=item.progress.position;lastPosition=player.currentTime}});
 player.on('video:error',()=>{if(current(g,player))$('#play-note').textContent='浏览器无法解码这份视频。请点击“本机播放器打开”，文件无需重下。'});
 player.on('video:timeupdate',()=>{if(current(g,player)&&Date.now()-lastSave>15000){lastSave=Date.now();saveProgress().catch(()=>{})}});
 player.on('video:pause',()=>{if(current(g,player))saveProgress().catch(()=>{})});
 loadSubtitles(g,player,id);autoDanmu(g,player,id);
}
async function loadSubtitles(g,player,id){
 try{const d=await api('/media/'+id+'/subtitles');if(!current(g,player))return;
  subtitleTracks=d.tracks;const supported=d.tracks.filter(t=>t.supported);
  $('#subtitle-choice').innerHTML='<option value="">关闭</option>'+d.tracks.map(t=>`<option value="${t.index}" ${t.supported?'':'disabled'}>${esc(t.label)}${t.supported?'':'（图形字幕，需弹弹play）'}</option>`).join('');
  $('#subtitle-choice').disabled=!supported.length;
  if(d.default!==null){$('#subtitle-choice').value=String(d.default);await changeSubtitle()}
  else $('#subtitle-note').textContent=d.tracks.length?'内嵌的是图形字幕，请用弹弹play显示。':'未发现内嵌字幕；画面自带的字幕不受此设置影响。';
 }catch(e){if(current(g,player))$('#subtitle-note').textContent=e.message}
}
async function changeSubtitle(){
 const g=generation,player=art,r=++subtitleRevision,value=$('#subtitle-choice').value;
 if(!player)return;
 if(value===''){player.subtitle.show=false;$('#subtitle-note').textContent='字幕已关闭';return}
 const track=subtitleTracks.find(t=>String(t.index)===value);if(!track)return;
 player.subtitle.show=false;$('#subtitle-note').textContent='正在加载'+track.label+'…';
 try{
  const response=await fetch(track.url);if(!response.ok){const d=await response.json();throw Error(d.error||d.detail||'字幕加载失败')}
  const text=await response.text();if(!text.startsWith('WEBVTT'))throw Error('字幕格式无效');
  subtitleQueue=subtitleQueue.catch(()=>{}).then(async()=>{
   if(!current(g,player)||r!==subtitleRevision)return;
   await player.subtitle.switch(track.url,{type:'vtt',name:track.label});
    if(!current(g,player)||r!==subtitleRevision){player.subtitle.show=false;return}
    player.subtitle.show=true;
    $('#subtitle-note').textContent='已加载'+track.label+(['ass','ssa'].includes(track.codec)?' · 使用文字字幕，原特效排版不保留':'');
  });await subtitleQueue;
 }catch(e){if(current(g,player)&&r===subtitleRevision)$('#subtitle-note').textContent=e.message}
}
function renderSelected(){$('#selected-sources').innerHTML=selected.map((x,i)=>`<p>${esc(x.title)} <button data-remove="${i}" class="outline small">移除</button></p>`).join('')}
async function applyDanmu(player=art){if(!player)throw Error('请先选择视频');const shift=Number($('#offset').value)||0;await player.plugins.artplayerPluginDanmuku.load(comments.map(c=>({...c,time:Math.max(0,c.time+shift)})))}
async function autoDanmu(g=generation,player=art,id=media?.id){
 if(!player||!id)return;const revision=++danmuRevision;$('#danmu-note').textContent='正在自动查找本集弹幕…';
 try{const d=await api('/media/'+id+'/danmu');if(!current(g,player)||revision!==danmuRevision)return;
  if(d.status==='matched'){selected=d.selected;comments=d.comments;renderSelected();await applyDanmu(player)}
  if(current(g,player)&&revision===danmuRevision)$('#danmu-note').textContent=d.message+(d.title?' · '+d.title:'');
 }catch(e){if(current(g,player)&&revision===danmuRevision)$('#danmu-note').textContent=e.message+'，可重新查找或手动选择来源。'}
}
document.addEventListener('click',async ev=>{
 const b=ev.target.closest('button');if(!b)return;b.disabled=true;const g=generation,player=art;
 try{
  if(b.dataset.media)await open(b.dataset.media);
  if(b.id==='retry-danmu')await autoDanmu();
  if(b.dataset.anime){const revision=++danmuRevision;const d=await api('/danmu/show/'+b.dataset.anime);if(g!==generation||revision!==danmuRevision)return;activeShow=d.bangumi;$('#danmu-episodes').innerHTML=`<p>${esc(activeShow.animeTitle)}</p><select id="episode-choice">${activeShow.episodes.map((x,i)=>`<option value="${i}">${esc(x.episodeTitle)}</option>`).join('')}</select><button id="add-source">添加这集的弹幕来源</button>`;}
  if(b.id==='add-source'){++danmuRevision;if(!media)throw Error('请先选择本机视频');if(selected.length>=5)throw Error('最多合并5个来源');const e=activeShow.episodes[Number($('#episode-choice').value)];if(!e.source_key)throw Error('此来源尚不支持网页直接加载');if(!selected.some(x=>x.id===e.source_key))selected.push({id:e.source_key,title:activeShow.animeTitle+' · '+e.episodeTitle});renderSelected();}
  if(b.dataset.remove!==undefined){++danmuRevision;selected.splice(Number(b.dataset.remove),1);renderSelected();$('#danmu-note').textContent='来源已变更，点击“加载 / 刷新”应用。';}
  if(b.id==='load-danmu'){
   if(!art)throw Error('请先选择视频');const revision=++danmuRevision,sources=[...selected];$('#danmu-note').textContent='正在获取弹幕…';
   const d=await api('/danmu/comments',{episodes:sources.map(x=>x.id)});if(!current(g,player)||revision!==danmuRevision)return;
   comments=d.comments;await applyDanmu(player);if(current(g,player)&&revision===danmuRevision)$('#danmu-note').textContent=`已加载 ${comments.length} 条 · `+d.sources.map((s,i)=>`${sources[i]?.title||'来源'}：${s.error||s.count+'条'}`).join('；');
  }
  if(b.id==='desktop'&&media){await api('/media/'+media.id+'/desktop',{});if(g===generation)$('#play-note').textContent='已请求弹弹play打开。网页未标记看完。'}
  if(b.id==='resume'&&art&&media.progress){art.currentTime=media.progress.position;lastPosition=art.currentTime;}
 }catch(e){if(g===generation||b.dataset.media)$('#danmu-note').textContent=e.message}finally{b.disabled=false}
});
$('#danmu-search').addEventListener('submit',async e=>{
 e.preventDefault();const g=generation,revision=++danmuRevision,button=e.target.querySelector('button');button.disabled=true;$('#danmu-note').textContent='正在搜索平台…';
 try{const d=await api('/danmu/search?q='+encodeURIComponent(e.target.query.value.trim()));if(g!==generation||revision!==danmuRevision)return;$('#danmu-results').innerHTML=(d.animes||[]).map(x=>`<button data-anime="${x.animeId}" class="outline">${esc(x.animeTitle)} · ${x.episodeCount}集</button>`).join('');$('#danmu-note').textContent=`找到 ${d.animes?.length||0} 个版本，请核对日语/国语、季度和集数。`}catch(err){if(g===generation&&revision===danmuRevision)$('#danmu-note').textContent=err.message}finally{button.disabled=false}
});
$('#subtitle-choice').addEventListener('change',changeSubtitle);
$('#media-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>list().catch(console.error),250)});
$('#offset').addEventListener('change',()=>applyDanmu().catch(e=>{$('#danmu-note').textContent=e.message}));
(async()=>{const bootstrap=await(await fetch('/api/bootstrap')).json();token=bootstrap.token;if(!bootstrap.local)$('#desktop').hidden=true;await list();const id=new URLSearchParams(location.search).get('media');if(id)await open(id)})().catch(e=>{$('#play-note').textContent=e.message});
