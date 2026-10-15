'use strict';
(async()=>{
  shell('subtitles');
  const video=$('#video'), player=$('#finished-player'), seek=$('#seek');
  const clock=t=>`${String(Math.floor((t||0)/60)).padStart(2,'0')}:${String(Math.floor((t||0)%60)).padStart(2,'0')}`;
  const message=text=>$('#message').textContent=text;
  function update(){
    const duration=Number.isFinite(video.duration)?video.duration:0;
    seek.max=duration || 1;seek.value=video.currentTime;
    $('#clock').textContent=`${clock(video.currentTime)} / ${clock(duration)}`;
    $('#play').textContent=video.paused?'播放':'暂停';
    $('#mute').textContent=video.muted?'声音关':'声音开';
    $('#mute').setAttribute('aria-label',video.muted?'开启声音':'静音');
  }
  async function toggle(){try{if(video.paused){if(video.ended)video.currentTime=0;await video.play();}else video.pause();}catch(e){message('播放失败，请使用“下载整集视频”在本机播放器打开。');}}
  const jump=delta=>{video.currentTime=Math.max(0,Math.min(video.duration||0,video.currentTime+delta));};
  try{
    await boot();
    const id=new URLSearchParams(location.search).get('release');
    const releases=await FinishedReleases.load(), release=releases.find(r=>r.id===id);
    if(!release)throw Error('没有找到这个成品，请返回字幕库选择。');
    const record=await api('/finished-viewing/'+id);
    let finished=record.finished, played=false, moved=false, lastPosition=0, lastSave=Date.now(), sequence=0;
    let queue=Promise.resolve();
    const session=crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(24)),x=>x.toString(16).padStart(2,'0')).join('');
    const endpoint='/api/finished-viewing/'+id;
    const tracking=text=>$('#tracking-status').textContent=text;
    function watchLabel(){const b=$('#watch-toggle');b.textContent=finished?'已看 · 改为未看':'标记看完';b.setAttribute('aria-pressed',String(finished));}
    async function post(path,body,keepalive=false){
      const response=await fetch(endpoint+path,{method:'POST',keepalive,headers:{'Content-Type':'application/json','X-Anime-Token':App.token},body:JSON.stringify(body)});
      if(!response.ok)throw Error('观看记录未同步，请刷新页面后重试');
      return response.json();
    }
    function save(keepalive=false){
      if(!played || !Number.isFinite(video.duration) || video.duration<=0)return Promise.resolve();
      const body={position:video.currentTime,duration:video.duration,playing:moved,session,sequence:++sequence};moved=false;
      const run=()=>post('/progress',body,keepalive).then(()=>tracking(`续播进度已同步 · ${clock(body.position)}`)).catch(e=>{moved ||= body.playing;tracking(e.message);});
      // Serialize normal writes; the server rejects an older in-flight write after pagehide.
      if(keepalive)return run();
      queue=queue.then(run);return queue;
    }
    const lease=(closed=false)=>post('/viewing-session',{session,closed},closed).catch(e=>{if(!closed)tracking(e.message);});
    let leaseTimer;
    function startLease(){clearInterval(leaseTimer);lease();leaseTimer=setInterval(()=>lease(),30000);}
    startLease();
    addEventListener('pagehide',()=>{save(true);clearInterval(leaseTimer);lease(true);});
    addEventListener('pageshow',e=>{if(e.persisted)startLease();});
    document.addEventListener('visibilitychange',()=>{if(document.hidden)save(true);});
    $('#watch-toggle').addEventListener('click',async()=>{
      const b=$('#watch-toggle');b.disabled=true;
      try{await queue;await post('/watch',{finished:!finished});finished=!finished;watchLabel();message(finished?'本集已同步为已看。':'本集已改为未看。');}
      catch(e){tracking(e.message);}finally{b.disabled=false;}
    });
    $('#show-link').href='/show/'+release.show_id;
    watchLabel();
    video.addEventListener('loadedmetadata',()=>{
      const p=record.progress;
      if(!finished && p?.position>5 && p.position<video.duration-30){video.currentTime=p.position;lastPosition=p.position;tracking(`已恢复本版本进度 · ${clock(p.position)}`);}
    },{once:true});
    video.addEventListener('seeked',()=>{lastPosition=video.currentTime;});
    video.addEventListener('timeupdate',()=>{
      const delta=video.currentTime-lastPosition;lastPosition=video.currentTime;
      if(!video.seeking && !video.paused && delta>0 && delta<35){played=true;moved=true;}
      if(Date.now()-lastSave>=15000){lastSave=Date.now();save();}
    });
    video.addEventListener('pause',()=>save());
    video.addEventListener('ended',()=>{save();if(!finished)message('本集播放结束，点击“标记看完”同步到追番记录。');});
    document.title=`${release.title} · 第${release.episode}集 · 番屿`;
    $('#release-title').textContent=`${release.title} · 第${release.episode}集`;
    $('#release-info').textContent=`${FinishedReleases.label(release)} · ${release.height?release.height+'p':'原画幅'} · ${clock(release.duration_seconds)} · 日语原声 · 画面已带字幕`;
    $('#download-video').href=release.video_url;$('#download-subtitles').href=release.subtitles_url;$('#release-notes').href=release.notes_url;
    video.src=release.video_url;video.volume=.8;video.controls=false;
    player.hidden=false;$('#controls').hidden=false;$('#release-links').hidden=false;
    for(const event of ['timeupdate','loadedmetadata','play','pause','ended','volumechange'])video.addEventListener(event,update);
    video.addEventListener('error',()=>message('视频读取失败，可下载整集后在本机播放器观看。'));
    $('#play').addEventListener('click',toggle);video.addEventListener('click',toggle);
    $('#back').addEventListener('click',()=>jump(-5));$('#forward').addEventListener('click',()=>jump(5));
    seek.addEventListener('input',()=>{video.currentTime=Number(seek.value);});
    $('#volume').addEventListener('input',()=>{video.volume=Number($('#volume').value);video.muted=false;});
    $('#mute').addEventListener('click',()=>{video.muted=!video.muted;});
    $('#rate').addEventListener('change',()=>{video.playbackRate=Number($('#rate').value);});
    $('#fullscreen').addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await player.requestFullscreen();}catch{message('当前窗口不支持全屏，可下载视频观看。');}});
    document.addEventListener('keydown',e=>{if(e.target.closest('input,select,button,a'))return;if(e.code==='Space'){e.preventDefault();toggle();}else if(e.code==='ArrowLeft'){e.preventDefault();jump(-5);}else if(e.code==='ArrowRight'){e.preventDefault();jump(5);}});
    update();
  }catch(e){$('#release-title').textContent='成品暂时无法打开';message(e.message);}
})();
