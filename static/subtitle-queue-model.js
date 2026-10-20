'use strict';
/* Production queue view model shared by the queue page and the show page: pure functions, no DOM. */
(function (root) {
  const STATUS = {producing: '制作中', starting: '正在启动制作', queued: '排队中', preparing: '准备素材', downloading: '视频下载中',
    waiting_video: '等待视频', blocked: '已暂停', v0_ready: 'v0 可看', done: 'v1 已完成'};
  const STAGE = {writing: '整集文字校订', screen_text: '画面字翻译', lyrics: 'OP / ED 歌词', placement: '字幕位置避让', effects: '必要动效',
    refining: '语义与时轴细调', layout: '排版与画面检查', rendering: '整集视频输出', checking: '解码与播放检查', publishing: '上架交付'};
  const REASON = {needs_verified_completed_media: '等完整可用视频', completed_media_needs_inventory_or_reconcile: '待核对已下载视频的归属',
    needs_completed_manifest_and_ownership: '待核对已下载视频的归属', target_changed_or_missing: '需重新匹配视频',
    needs_reliable_chinese_dialogue_base: '缺可用中文字幕原稿', archived_input_changed_or_invalid: '原稿文件待核对',
    pending_inventory: '视频已到，待盘点字幕', readiness_proof_or_exact_input_missing: '就绪证明待补',
    refinement_baseline_or_target_changed: 'v0 基线或视频已变化，待重新核对',
    production_blocked: '负责人已记录具体阻塞，等待核验处理', scope_protected: '个人范围或授权保护',
    preparation_check_required: '准备证据待核验'};
  Object.assign(REASON, {needs_actual_clean_mother_review: '待检查制作母片',
    needs_verified_local_finished_output_tools: '待核验字幕输出工具',
    needs_reliable_japanese_dialogue_or_real_transcription: '日文对白材料待核验',
    needs_actual_audio_picture_and_player_capability: '音画与播放能力待核验',
    late_review_target_changed: '修订媒体已变化，待重新核对'});
  const GAP = {download_ready: '已发现资源，后台正在入队', download_retry: '获取种子失败，等待重试', candidate_review: '候选待核验',
    quality_required: '候选不符合画质要求', primary_window: '等待主源发布', source_not_found: '尚未发现本集资源', sources_disabled: '没有启用的订阅源'};
  const rel = s => s < 60 ? '刚刚' : s < 3600 ? `${Math.floor(s / 60)} 分钟前` : s < 86400 ? `${Math.floor(s / 3600)} 小时前` : `${Math.floor(s / 86400)} 天前`;
  const span = s => s < 3600 ? `${Math.max(1, Math.round(s / 60))} 分钟` : s < 86400 ? `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} 小时` : `${(s / 86400).toFixed(1)} 天`;
  const when = t => { const d = new Date(t * 1000); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
  const reasons = item => (item.reasons || []).map(r => REASON[r] || r).join('；');

  function describe(item, now = Date.now() / 1000) {
    const d = describeStatus(item, now);
    return item.priority_lane ? {...d, label: `优先通道 · ${d.label}`} : d;
  }

  function describeStatus(item, now) {
    switch (item.status) {
      case 'producing': {
        if (item.stalled) {
          const seen = span(item.stalled_seconds);
          const detail = item.owner_state === 'idle' ? `负责会话已空闲 ${seen}；等待调度按规则恢复，仍无进展就需要你处理`
            : item.owner_state === 'stuck' ? `负责会话空挂 ${seen}，守卫记录需核验；按既有规则处理`
            : `${seen}没有进度回报；需核验原负责人状态，按既有规则恢复`;
          return {label: `可能卡住 · ${item.edition}`, detail, tone: 'warn'};
        }
        const stage = STAGE[item.stage];
        const detail = stage ? `${stage}${item.progress ? ` ${item.progress.done}/${item.progress.total}` : ''} · ${rel(now - item.last_activity)}回报`
          : item.owner_state === 'working' ? `负责会话工作中 · 最近活动 ${rel(now - item.owner_last_activity)}`
          : `负责人已开始 · 开始于 ${rel(now - (item.since || now))}`;
        return {label: `制作中 · ${item.edition}`, detail, tone: 'signal'};
      }
      case 'starting':
        return item.stalled ? {label: '启动回执未确认', detail: `${span(item.stalled_seconds)}没有确认；等待调度核验原启动结果`, tone: 'warn'}
          : {label: STATUS.starting, detail: '等待负责会话的启动回执', tone: 'signal'};
      case 'queued':
        return {label: `排队第 ${item.position} 位`, detail: item.ahead ? `前面还有 ${item.ahead} 集；一次只做一集` : '制作槽空闲，等待下一轮调度核验', tone: 'muted'};
      case 'preparing': return {label: STATUS.preparing, detail: reasons(item) || '后台核对素材中', tone: 'muted'};
      case 'downloading': return {label: `视频下载中 ${item.download_progress ?? 0}%`, detail: '下载完成后核对归属并盘点字幕', tone: 'muted'};
      case 'waiting_video': {
        const g = item.gap;
        return {label: STATUS.waiting_video, detail: g ? `${GAP[g.code] || g.reason || ''}${g.retry_at ? ` · ${when(g.retry_at)} 重试` : ''}` : '后台尚未发现本集资源', tone: 'muted'};
      }
      case 'blocked': return {label: STATUS.blocked, detail: reasons(item) || '等待补齐缺件后由同一负责人继续', tone: 'warn'};
      case 'v0_ready': {
        const r = item.refinement;
        if (!r) return {label: STATUS.v0_ready, detail: 'v1 精校尚未排入', tone: 'ok'};
        if (r.status === 'queued') return {label: STATUS.v0_ready, detail: r.position ? `v1 精校排队第 ${r.position} 位${r.ahead ? `，前面还有 ${r.ahead} 集` : ''}` : 'v1 精校排队中', tone: 'ok'};
        const d = describe(r, now);
        return {label: STATUS.v0_ready, detail: `v1 ${d.label}${d.detail ? ' · ' + d.detail : ''}`, tone: d.tone === 'warn' ? 'warn' : 'ok'};
      }
      case 'done': return {label: STATUS.done, detail: '可直接观看', tone: 'ok'};
    }
    return {label: STATUS[item.status] || item.status, detail: '', tone: 'muted'};
  }

  const active = i => ['producing', 'starting'].includes(i.status) || ['producing', 'starting'].includes(i.refinement?.status);
  function sections(queue, now) {
    const items = queue.items || [];
    const attention = items.filter(i => i.stalled || i.refinement?.stalled || i.status === 'blocked');
    const pick = f => items.filter(i => !attention.includes(i) && f(i));
    return [
      {key: 'attention', title: '需要留意', items: attention},
      {key: 'active', title: '正在制作', items: pick(active)},
      {key: 'queued', title: '排队中', items: pick(i => i.status === 'queued')},
      {key: 'preparing', title: '准备中', items: pick(i => ['preparing', 'downloading'].includes(i.status))},
      {key: 'waiting', title: '等待视频', items: pick(i => i.status === 'waiting_video')},
      {key: 'ready', title: '可以观看', items: pick(i => ['v0_ready', 'done'].includes(i.status) && !active(i))},
    ].filter(s => s.items.length);
  }

  function line(item, now) { const d = describe(item, now); return `字幕 · ${d.label}${d.detail ? ' · ' + d.detail : ''}`; }
  /* How long a playable video has waited for its own subtitles; empty once v1 is done or no video exists yet. */
  function waited(item) { return item.waiting_seconds > 0 && item.status !== 'done' ? `视频到位 ${span(item.waiting_seconds)}` : ''; }

  function health(queue, now = Date.now() / 1000) {
    const parts = [], s = queue.scheduler, slot = queue.slot || {};
    parts.push(slot.occupied ? slot.item ? `制作槽：${slot.item.title} 第 ${slot.item.episode} 集（${slot.item.edition}${slot.item.stalled ? '，可能卡住' : ''}）` : '制作槽由保护中的任务占用，等待核验' : '制作槽空闲');
    if (s) parts.push(s.ok ? `调度检查 ${rel(now - s.checked_at)}${s.last_activity ? ` · 调度会话最近活动 ${rel(now - s.last_activity)}` : ''}${s.stale ? '（守卫记录已过时）' : ''}` : '调度守卫读取异常');
    return parts.join(' · ');
  }

  const api = {STATUS, STAGE, describe, sections, line, waited, health};
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.SubtitleQueueModel = api;
})(typeof window === 'undefined' ? globalThis : window);
