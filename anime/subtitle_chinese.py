"""Traceable Chinese preparation from completed releases and archived discovery.

This module never downloads, changes the production target, or edits a workbook.
Burned-in subtitles and OCR observations are useful inputs, not ready transcripts.
"""
import hashlib
from contextlib import contextmanager
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3
import time

from .download_receipts import pending_receipt
from .fidelity import fidelity_blocker
from .subtitle_targets import exists, head, proof

PIPELINE = 'chinese-sources-v2'
INSPECTION_VERSION = 'explicit-und-track-title-v1'
SUB_EXTENSIONS = {'.ass', '.ssa', '.srt', '.vtt'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _private_file(root, ref, limit=16 * 1024 * 1024):
    path = Path(ref['path']).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Chinese source evidence must be a bounded private file')
    raw = path.read_bytes()
    if digest(raw) != ref['sha256']:
        raise ValueError('Chinese source evidence changed')
    return path, raw


@lru_cache(maxsize=64)
def _small_cue_count(parser, raw, kind):
    """Only pure parsing is reused; every caller still verifies current bytes."""
    return len(parser(raw, kind))


def archived_cue_count(parser, raw, kind):
    # Bound retained bytes to at most 64 MiB, independent of archive size.
    if len(raw) <= 1024 * 1024:
        return _small_cue_count(parser, raw, kind)
    return len(parser(raw, kind))


def archived_chinese_sources(db, sid, ep):
    root = db.path.parent / 'subtitle-studio'
    path = root / 'archive.sqlite3'
    if not path.is_file():
        return []
    from .subtitle_lab import lab_core
    core = lab_core()
    key = core.episode_key(sid, db.mapping(sid).get('season', 1), ep)
    result = []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as con:
        con.row_factory = sqlite3.Row
        for row in con.execute('SELECT id,raw_hash,text_hash,language,metadata FROM drafts WHERE episode_key=? ORDER BY id', (key,)):
            try:
                meta = json.loads(row['metadata'])
                if row['language'] not in core.ZH or fidelity_blocker(db, meta.get('release', '')):
                    continue
                source, raw = _private_file(root, {'path': meta['raw_file'], 'sha256': row['raw_hash']})
                cue_count = archived_cue_count(core.parse_subtitles, raw, source.suffix.lstrip('.'))
                estimated = bool(meta.get('timing_is_estimated'))
                checkpoint = _correction_progress(root, meta, row['raw_hash'], sid, season=db.mapping(sid).get('season', 1), ep=ep)
                result.append({'kind': 'archived_draft', 'draft_id': row['id'], 'path': str(source),
                    'sha256': row['raw_hash'], 'text_hash': row['text_hash'], 'cue_count': cue_count,
                    'origin': meta.get('origin'), 'media_fingerprint': meta.get('media_fingerprint'),
                    'estimated': estimated, 'mixed': bool(meta.get('mixed_language_suspected')),
                    'candidate_not_reliable_base': bool(meta.get('candidate_not_reliable_base')),
                    'state': 'ocr_needs_correction_and_alignment' if estimated else
                             'mixed_track_needs_separation' if meta.get('mixed_language_suspected') else 'text_archived',
                    'frame_extraction': meta.get('frame_extraction'), 'raw_ocr': meta.get('raw_ocr'),
                    'target_matching_status': meta.get('target_matching_status'),
                    'target_fingerprint': meta.get('target_fingerprint'),
                    'correction_progress': checkpoint})
            except (ValueError, KeyError, TypeError, OSError):
                continue
    return result


def _correction_progress(root, meta, raw_hash, sid, season, ep):
    """Read a bound partial checkpoint for routing only, never qualification.

    The original OCR remains estimated. A corrected candidate with unresolved
    coverage must advance to alignment instead of repeatedly correcting it.
    Mutable RESULT.json files are deliberately not used as evidence.
    """
    related = meta.get('related_outputs') or {}
    if not isinstance(related, dict):
        return None
    ref = related.get('candidate-checkpoint.json') or related.get('review.partial.json')
    if not ref or not meta.get('candidate_not_reliable_base'):
        return None
    try:
        _, raw = _private_file(root, ref, 8 * 1024 * 1024)
        report = json.loads(raw)
        if not isinstance(report, dict):
            return None
        corrected = report.get('corrected_subtitle') or {}
        if report.get('schema') == 'chinese-source-candidate-checkpoint/v1':
            from .subtitle_chinese_intake import review_summary
            if ((report.get('show_id'), report.get('season'), report.get('episode')) != (sid, season, ep)
                    or report.get('source_media_fingerprint') != meta.get('media_fingerprint')
                    or corrected.get('sha256') != raw_hash or report.get('production_ready_claimed') is not False):
                return None
            _private_file(root, corrected)
            _, reviewed_raw = _private_file(root, report['review_report'], 8 * 1024 * 1024)
            reviewed = json.loads(reviewed_raw)
            summary = review_summary(reviewed, sid, season, ep, meta.get('media_fingerprint'), raw_hash,
                                     meta.get('source_media_sha256'))
            _private_file(root, reviewed['corrected_subtitle'])
            return {**summary, 'report': ref}
        hardtext = report.get('source_hardtext_review') or {}
        if (not isinstance(corrected, dict) or not isinstance(hardtext, dict)
                or report.get('schema') != 'chinese-dialogue-review/v1'
                or report.get('reviewer_kind') != 'codex'
                or (report.get('show_id'), report.get('season'), report.get('episode')) != (sid, season, ep)
                or hardtext.get('status') != 'complete'
                or report.get('target_fingerprint') != meta.get('media_fingerprint')
                or corrected.get('sha256') != raw_hash):
            return None
        _private_file(root, corrected)
        whole = report.get('whole_episode_review') or {}
        if not isinstance(whole, dict):
            return None
        return {'source_hardtext_review': 'complete', 'report': ref,
                'next_action': 'align_chinese_source',
                'pending_reference_items': whole.get('known_Japanese_reference_items_without_verified_Chinese_mapping'),
                'production_ready_claimed': False}
    except (ValueError, KeyError, TypeError, OSError):
        return None


def same_video_sidecars(task, ep):
    """A season pack's E2 subtitle must never be archived as E3."""
    selection = json.loads(task['selection'])
    video = selection.get('selected', {}).get(str(ep))
    if not video:
        return []
    root = Path(task['save_path']).resolve()
    media = (root / video['name']).resolve()
    result = []
    for entry in selection.get('files', []):
        path = (root / entry['name']).resolve()
        if (entry['index'] not in selection.get('ids', []) or path.suffix.lower() not in SUB_EXTENSIONS
                or not path.is_relative_to(root) or path.parent != media.parent
                or not path.stem.startswith(media.stem + '.') or not path.is_file()
                or path.stat().st_size != entry['size'] or not 0 < entry['size'] <= 8 * 1024 * 1024):
            continue
        hint = path.stem[len(media.stem) + 1:].lower()
        zh = re.search(r'(?<![a-z])(?:sc|tc|chs|cht|chi|zho|zh(?:-hans|-hant|-cn|-tw)?)(?![a-z])|简体|繁体|繁體|中文', hint)
        ja = re.search(r'(?<![a-z])(?:ja|jpn|jp)(?![a-z])|日语|日文|日本語', hint)
        result.append({'index': entry['index'], 'path': str(path), 'sha256': digest(path.read_bytes()),
                       'language': 'chi' if zh and not ja else 'jpn' if ja and not zh else 'und'})
    return result


def completed_sources(db, sid, ep):
    from .subtitle_lab import lab_core
    result = []
    for task in db.rows("SELECT * FROM tasks WHERE show_id=? AND status IN ('complete','linked') ORDER BY hash", (sid,)):
        try:
            if task.get('purpose') not in ('episode', 'source_sample', 'subtitle_sample'):
                continue
            if fidelity_blocker(db, task['title'], task['selection']):
                continue
            queue = db.one('SELECT state,lease,committing FROM thunder_queue WHERE hash=?', (task['hash'],))
            if pending_receipt(task, queue) or db.get('thunder_dialog_hold_' + task['hash'], {}).get('unconfirmed'):
                continue
            selected = json.loads(task['selection']).get('selected', {}).get(str(ep))
            if not selected:
                continue
            root = Path(task['save_path']).resolve()
            path = (root / selected['name']).resolve()
            if not path.is_relative_to(root):
                continue
            if task['status'] == 'linked':
                receipt = db.get('thunder_readonly_' + task['hash'], {})
                if (receipt.get('mode') != 'read_only' or receipt.get('hash') != task['hash']
                        or receipt.get('size') != selected['size']):
                    continue
                path = Path(receipt['path']).resolve()
            if not path.is_file() or path.stat().st_size != selected['size']:
                continue
            item = {'kind': 'completed_media', 'show_id': sid, 'episode': ep,
                'hash': task['hash'], 'title': task['title'], 'path': str(path), 'size': selected['size'],
                'fingerprint': lab_core().fingerprint(path, selected['size']),
                'state': 'local_track_or_burned_subtitle_inspection_pending',
                'sidecars': same_video_sidecars(task, ep)}
            receipt = _inspection_path(db, item)
            if receipt.is_file():
                record = json.loads(receipt.read_text(encoding='utf-8'))
                if (record.get('fingerprint') == item['fingerprint']
                        and record.get('inspection_version') == INSPECTION_VERSION):
                    item.update(state=record['state'], inspection_path=str(receipt),
                                inspection_sha256=digest(receipt.read_bytes()))
            result.append(item)
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return result


def _inspection_path(db, item):
    # Only the interpretation of native track declarations changed. Keep old
    # reports immutable and leave the preparation pipeline/attempt ledger alone.
    revision = digest(json.dumps([PIPELINE, INSPECTION_VERSION, item['fingerprint'], item['sidecars']], sort_keys=True).encode())
    return db.path.parent / 'subtitle-studio' / 'chinese-source-inspections' / f"{item['show_id']}-{item['episode']}" / (revision + '.json')


def _recheck_completed_source(db, item):
    current = next((r for r in completed_sources(db, item['show_id'], item['episode'])
                    if r['hash'] == item['hash']), None)
    if (not current or current['fingerprint'] != item['fingerprint']
            or current['sidecars'] != item['sidecars']):
        raise ValueError('Completed source ownership, receipt or sidecar changed during preparation')


def discovery_sources(db, sid, ep):
    """Read bounded, episode-specific preparation handoffs, checking actual bytes."""
    root = db.path.parent / 'subtitle-studio'
    season = db.mapping(sid).get('season', 1)
    suffix = f'{sid}-S{season:02}E{ep:02}/RESULT.json'
    reports = sorted(root.glob('parallel-*/subtitle-discovery/' + suffix))
    reports += sorted(root.glob('source-discovery/' + suffix))
    if len(reports) > 100:
        raise ValueError('Chinese discovery history needs archival')
    result = []
    for report_path in reports:
        try:
            if report_path.stat().st_size > 1048576:
                continue
            report = json.loads(report_path.read_text(encoding='utf-8-sig'))
            if (report.get('show_id'), report.get('season'), report.get('episode')) != (sid, season, ep):
                continue
            for source in report.get('sources', []):
                if not isinstance(source, dict) or not source.get('actual_language'):
                    continue
                path, raw = _private_file(root, source)
                if path.suffix.lower() not in SUB_EXTENSIONS:
                    continue
                title = source.get('original_filename', path.name)
                if fidelity_blocker(db, title):
                    continue
                language = source['actual_language'].strip().lower()
                # These are affirmative labels, not a substring classifier:
                # "Japanese story dialogue ...; not Chinese" is Japanese.
                chinese = bool(re.match(r'^chinese(?:\b|$)', language))
                japanese = bool(re.match(r'^japanese(?:\b|$)', language))
                bilingual = bool(re.match(r'^(?:chinese and japanese|japanese and chinese)\b', language))
                if not (chinese or japanese):
                    continue
                result.append({'kind': 'discovery_source', 'path': str(path), 'sha256': digest(raw),
                    'title': title, 'url': source.get('url'), 'source_key': source.get('url') or str(report_path),
                    'language': 'chi' if chinese or bilingual else 'jpn',
                    'bilingual': bilingual,
                    'state': 'archived_file_not_registered', 'provenance_report': str(report_path),
                    'provenance_sha256': digest(report_path.read_bytes()),
                    'target_match_status': source.get('target_match_status', 'alignment_pending'),
                    'declared_credits': source.get('declared_credits'),
                    'declared_author': source.get('declared_author')})
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return result


def chinese_preparation(db, sid, ep, target=None):
    """Read-only live input revision, independent of queue refresh timestamps."""
    drafts = archived_chinese_sources(db, sid, ep)
    media = completed_sources(db, sid, ep)
    discovered = discovery_sources(db, sid, ep)
    registered = {r['sha256'] for r in drafts}
    pending = [r for r in discovered if r['language'] == 'chi' and r['sha256'] not in registered]
    # Prefer an existing proper text source over re-running/correcting OCR.
    useful = [r for r in drafts if not r['estimated'] and not r['mixed'] and not r['candidate_not_reliable_base']
              and ('ocr' not in str(r['origin']).lower() or r['origin'] == 'codex-reviewed-ocr')]
    # A completed source-clock correction is still useful when the production
    # target differs: matching it is exactly the next action, not a reason to
    # repeat the original OCR correction. This never qualifies it as a base.
    corrected_states = ('complete', 'recovered_cues_corrected')
    corrected_pending = [r for r in drafts if r.get('correction_progress')
                         and r['correction_progress'].get('source_hardtext_review') in corrected_states]
    partial_correction = any(r.get('correction_progress')
                             and r['correction_progress'].get('source_hardtext_review') not in corrected_states
                             for r in drafts)
    if pending:
        action = 'collect_chinese_source'
    elif useful or corrected_pending:
        action = 'align_chinese_source'
    elif partial_correction or any(r['estimated'] for r in drafts):
        action = 'review_chinese_ocr'
    elif any(r['mixed'] for r in drafts) or any(r['state'] == 'local_track_or_burned_subtitle_inspection_pending' for r in media):
        action = 'collect_chinese_source'
    else:
        action = 'find_chinese'
    actions = [action]
    if partial_correction or (any(r['estimated'] for r in drafts) and not corrected_pending):
        actions.append('review_chinese_ocr')
    if useful or corrected_pending:
        actions.append('align_chinese_source')
    actions.append('find_chinese')
    candidates = pending + drafts + media
    def source_revision(sources):
        return digest(json.dumps({'pipeline': PIPELINE, 'sources': sources,
            'target': (target or {}).get('fingerprint')}, sort_keys=True, ensure_ascii=False).encode())
    # Inspection closes a local inventory step; it does not introduce a new
    # subtitle or media input and must not replenish an exhausted web search.
    stable = []
    for candidate in candidates:
        item = dict(candidate)
        if item.get('kind') == 'completed_media':
            item.pop('inspection_path', None)
            item.pop('inspection_sha256', None)
            item['state'] = 'local_track_or_burned_subtitle_inspection_pending'
        stable.append(item)
    revision = source_revision(stable)
    return {'action': action, 'actions': list(dict.fromkeys(actions)), 'revision': revision, 'pipeline': PIPELINE, 'candidates': candidates,
            'revision_aliases': sorted({revision, source_revision(candidates)}),
            'reliable_text_count': len(useful), 'ocr_candidate_count': sum(r['estimated'] for r in drafts),
            'production_ready_claimed': False}


def _ensure_unowned(db, sid, ep):
    from .viewing_workflow import production_wanted, RESEARCH_MODES
    if (not db.get('subtitle_studio_enabled', False) or not production_wanted(db, sid)
            or not db.review_wanted(sid) or not db.allowed(sid, 1) or not db.allowed(sid, ep)):
        raise ValueError('Chinese preparation is outside the authorized episode scope')
    policy = db.one('SELECT config FROM source_policies WHERE show_id=?', (sid,))
    if not policy or json.loads(policy['config']).get('mode') not in RESEARCH_MODES:
        raise ValueError('Chinese preparation has no research policy')
    if db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1', (sid, ep)):
        raise ValueError('Watched episodes retain their subtitles')
    if exists(db, 'subtitle_delivery_jobs') and db.one('''SELECT 1 FROM subtitle_delivery_jobs
            WHERE show_id=? AND episode=? AND (thread_id<>'' OR dispatch_state<>'' OR lease_owner<>'' OR lease_expires>?)''',
            (sid, ep, time.time())):
        raise ValueError('Existing owner or dispatch must retain its preparation')


@contextmanager
def _preparation_gate(db, sid, ep, guard):
    # A process-local lock alone cannot stop the original service assigning an
    # owner between checking scope and appending archive input. Hold the original
    # DB write reservation only for this short immutable archive operation.
    with db.gate, db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        _ensure_unowned(db, sid, ep)
        if guard:
            guard()
        yield
        con.rollback()


def archive_text_source(archive, core, sid, season, ep, path, metadata):
    """Retain a bilingual original, then split only explicitly labelled CN/JP styles.

    This is source extraction, not translation, target alignment or acceptance.
    """
    path = Path(path)
    ids = [archive.add_base(sid, season, ep, path, metadata)]
    if path.suffix.lower() not in ('.ass', '.ssa'):
        return ids
    raw = path.read_bytes()
    cues = core.parse_subtitles(raw, path.suffix.lstrip('.'))
    for language, pattern in (('chi', r'(?:^|[ _-])(?:cn|chs|cht|zh)(?:$|[ _-])|(?:op|ed)cn$'),
                              ('jpn', r'(?:^|[ _-])(?:jp|jpn|ja)(?:$|[ _-])|(?:op|ed)jp$')):
        selected = [c for c in cues if re.search(pattern, c['style'], re.I)]
        # Native, already separated tracks don't need duplicate derived drafts.
        if not selected or len(selected) == len(cues):
            continue
        if language == 'chi' and any(re.search(r'[\u3040-\u30ff]', c['text']) for c in selected):
            continue  # Conflicting labels require contextual review, not an automatic guess.
        directory = archive.root / 'derived-language-tracks' / digest(raw)
        directory.mkdir(parents=True, exist_ok=True)
        body = 'WEBVTT\n\n' + '\n\n'.join(f'{core.vtt_time(c["start_ms"])} --> {core.vtt_time(c["end_ms"])}\n{c["text"]}' for c in selected) + '\n'
        dest = directory / (language + '.vtt')
        encoded = body.encode('utf-8')
        if dest.exists() and dest.read_bytes() != encoded:
            raise ValueError('Immutable separated source changed')
        if not dest.exists():
            dest.write_bytes(encoded)
        ids.append(archive.add_base(sid, season, ep, dest, {**metadata, 'language': language,
            'track': str(metadata.get('track', 'source')) + '-' + language,
            'origin': 'explicit-ass-language-style', 'source_draft_id': ids[0],
            'source_raw_sha256': digest(raw), 'source_cue_ids': [c['id'] for c in selected],
            'source_styles': sorted({c['style'] for c in selected}),
            'extraction_method': 'explicit_CN_JP_ASS_style_preserve_text_and_timestamps',
            'target_matching_status': metadata.get('target_matching_status', 'pending')}))
    return ids


def _embedded_track_language(track):
    """Use an exact, single-language title only when no language was declared.

    A bracketed ASCII publisher credit may precede the label (e.g. Sakurato).
    Other title text, mixed labels and language-like credits stay unresolved.
    Display labels are deliberately not evidence for this inference.
    """
    declared = track.get('declared_language', track.get('language', ''))
    language = (declared or '').strip().lower()
    if language not in ('', 'und') or track.get('origin') == 'sidecar':
        return language or 'und', None
    title = track.get('title', '')
    if not isinstance(title, str):
        return 'und', None
    labels = {'chs': 'chs', 'cht': 'cht', 'chi': 'chi', 'zho': 'chi',
              'chinese': 'chi', '中文': 'chi', '简体中文': 'chs', '簡體中文': 'chs',
              '繁体中文': 'cht', '繁體中文': 'cht',
              'simplified chinese': 'chs', 'traditional chinese': 'cht',
              'zh-hans': 'chs', 'zh-hant': 'cht'}
    label = title.strip().lower()
    credited = re.fullmatch(r'\[([a-z0-9][a-z0-9_. -]{0,79})\]\s*(.+)', label)
    if credited:
        credit, label = credited.groups()
        if re.search(r'\b(?:chs|cht|chi|zho|zh|cn|sc|tc|jp|ja|jpn|en|eng|ko|kor|'
                     r'chinese|japanese|english|korean|bilingual|dual|multi)\b', credit):
            return 'und', None
    effective = labels.get(label)
    if not effective:
        return 'und', None
    return effective, {'method': 'explicit-und-track-title', 'version': 1,
                       'title_label': label, 'effective_language': effective}


def collect_chinese_sources(db, sid, ep, guard=None):
    """Explicit local repair entry; immutable archives only, no Engine/writer changes."""
    from .subtitle_studio import archive_for
    from .subtitle_lab import lab_core
    from .subtitles import Subtitles
    with _preparation_gate(db, sid, ep, guard):
        pass
    archive, core, sub = archive_for(db), lab_core(), Subtitles(db)
    season = db.mapping(sid).get('season', 1)
    before = chinese_preparation(db, sid, ep)
    ids, errors = [], []
    for source in discovery_sources(db, sid, ep):
        with _preparation_gate(db, sid, ep, guard):
            path, _ = _private_file(archive.root, source)
            _private_file(archive.root, {'path': source['provenance_report'],
                'sha256': source['provenance_sha256']}, 1048576)
            ids += archive_text_source(archive, core, sid, season, ep, path, {
                'source_key': source['source_key'], 'track': 'downloaded-source',
                'language': source['language'], 'release': source['title'], 'origin': 'archived-discovery',
                'source_url': source.get('url'), 'declared_credits': source.get('declared_credits'),
                'provenance_report': {'path': source['provenance_report'], 'sha256': source['provenance_sha256']},
                'media_fingerprint': None, 'target_matching_status': source['target_match_status']})
    for item in completed_sources(db, sid, ep):
        meta = {'source_key': item['hash'], 'release': item['title'], 'media_fingerprint': item['fingerprint']}
        try:
            source_ids = []
            tracks = []
            for track in sub.tracks(item):
                language, inference = _embedded_track_language(track)
                tracks.append({**track, 'effective_language': language, 'language_inference': inference})
            for track in tracks:
                language = track['effective_language']
                if language not in core.ZH | core.JA or not track['supported']:
                    continue
                fmt = 'ass' if track['codec'] in ('ass', 'ssa') else 'vtt'
                path = sub.extract(item, track['index'], fmt)
                with _preparation_gate(db, sid, ep, guard):
                    _recheck_completed_source(db, item)
                    if core.fingerprint(item['path'], item['size']) != item['fingerprint']:
                        raise ValueError('Source media changed during extraction')
                    source_ids += archive_text_source(archive, core, sid, season, ep, path,
                        {**meta, 'track': track['index'], 'language': language, 'origin': 'embedded',
                         'declared_language': track.get('declared_language', track.get('language', '')),
                         'track_title': track.get('title', ''), 'language_inference': track['language_inference']})
            for sidecar in item['sidecars']:
                if sidecar['language'] == 'und':
                    continue
                with _preparation_gate(db, sid, ep, guard):
                    _recheck_completed_source(db, item)
                    path = Path(sidecar['path'])
                    if digest(path.read_bytes()) != sidecar['sha256']:
                        raise ValueError('Source sidecar changed during extraction')
                    source_ids += archive_text_source(archive, core, sid, season, ep, path,
                        {**meta, 'track': 100000 + sidecar['index'], 'language': sidecar['language'], 'origin': 'original-sidecar'})
            ids += source_ids
            chinese = any(archive.draft(i)['language'] in core.ZH for i in source_ids)
            inspection = {'pipeline': PIPELINE, 'inspection_version': INSPECTION_VERSION,
                'fingerprint': item['fingerprint'],
                'state': 'chinese_text_archived' if chinese else 'no_chinese_text_track',
                'tracks': tracks, 'sidecars': item['sidecars'], 'draft_ids': sorted(set(source_ids)),
                'burned_subtitles': 'not_excluded_by_absence_of_text_tracks'}
            receipt = _inspection_path(db, item)
            with _preparation_gate(db, sid, ep, guard):
                _recheck_completed_source(db, item)
                receipt.parent.mkdir(parents=True, exist_ok=True)
                if not receipt.exists():
                    with receipt.open('x', encoding='utf-8') as stream:
                        json.dump(inspection, stream, ensure_ascii=False, indent=2)
        except (ValueError, OSError) as ex:
            errors.append({'source_hash': item['hash'], 'error': str(ex)[:300]})
    return {'pipeline': PIPELINE, 'show_id': sid, 'episode': ep, 'draft_ids': sorted(set(ids)),
            'errors': errors, 'before': before, 'after': chinese_preparation(db, sid, ep),
            'production_ready_claimed': False, 'target_changed': False, 'owner_changed': False}


def validate_chinese_review(db, sid, ep, report_path):
    """Validate Codex's actual frame corrections and target timing evidence.

    A report is not a toggle on the old estimated OCR. Every source observation
    and every corrected output cue must be accounted for in a new immutable file.
    The full-episode check includes OCR sampling gaps, which can hide short cues.
    """
    from .subtitle_studio import archive_for
    from .subtitle_lab import lab_core
    core, archive = lab_core(), archive_for(db)
    root, report_path = archive.root, Path(report_path).resolve()
    ref = {'path': str(report_path), 'sha256': digest(report_path.read_bytes())}
    _, raw = _private_file(root, ref, 8 * 1024 * 1024)
    report = json.loads(raw)
    if (report.get('schema') != 'chinese-dialogue-review/v1' or report.get('reviewer_kind') != 'codex'
            or (report.get('show_id'), report.get('season'), report.get('episode')) !=
               (sid, db.mapping(sid).get('season', 1), ep)):
        raise ValueError('Chinese review must identify this episode and Codex review')
    source = archive.draft(report['source_draft_id'])
    if source['episode_key'] != core.episode_key(sid, report['season'], ep) or source['raw_hash'] != report.get('source_raw_sha256'):
        raise ValueError('Chinese review source draft changed or belongs to another episode')
    if source['language'] not in core.ZH:
        raise ValueError('Chinese review must correct an actual Chinese source')
    from .subtitle_evidence import current_target
    target = current_target(db, sid, ep)
    if not target or report.get('target_fingerprint') != target['fingerprint']:
        raise ValueError('Chinese review must bind the current production target')
    if core.fingerprint(target['path'], target['size']) != target['fingerprint']:
        raise ValueError('Chinese review target changed')
    media = next((item for item in completed_sources(db, sid, ep)
                  if item['fingerprint'] == source['metadata'].get('media_fingerprint')), None)
    if not media or report.get('source_media_fingerprint') != media['fingerprint']:
        raise ValueError('Chinese review must bind the completed source media')
    corrected_path, corrected_raw = _private_file(root, report['corrected_subtitle'])
    corrected = core.parse_subtitles(corrected_raw, corrected_path.suffix.lstrip('.'))
    if sum(bool(re.search(r'[\u3040-\u30ff]', c['text'])) for c in corrected) / len(corrected) > .25:
        raise ValueError('Corrected Chinese transcript still contains a mixed Japanese track')
    frames = {}
    for frame in report.get('source_frames', []):
        _, pixels = _private_file(root, frame, 64 * 1024 * 1024)
        if not pixels.startswith(b'\x89PNG\r\n\x1a\n') or not isinstance(frame.get('seconds'), (int, float)):
            raise ValueError('Source frame evidence requires actual PNG and source time')
        if frame.get('id') in frames:
            raise ValueError('Source frame ids must be unique')
        frames[frame['id']] = frame
    source_cues = {c['id']: c for c in source['cues']}
    output_cues = {c['id']: c for c in corrected}
    seen, mapped = set(), set()
    for item in report.get('coverage', []):
        cue_id = item.get('source_cue_id')
        cue = source_cues.get(cue_id)
        if not cue or cue_id in seen or not str(item.get('evidence', '')).strip():
            raise ValueError('Chinese review needs each source cue once with actual correction evidence')
        seen.add(cue_id)
        observed = [frames[i] for i in item.get('source_frame_ids', []) if i in frames]
        if (not observed or len(observed) != len(item.get('source_frame_ids', []))
                or not any(cue['start_ms'] <= f['seconds'] * 1000 <= cue['end_ms'] for f in observed)):
            raise ValueError('Every source cue needs a frame in its source timing window')
        output = item.get('corrected_cue_ids') or []
        if item.get('status') == 'corrected_dialogue':
            if not output or any(i not in output_cues for i in output):
                raise ValueError('Corrected dialogue needs actual output cue mapping')
        elif item.get('status') == 'non_dialogue':
            if output or not item.get('exemption_reason'):
                raise ValueError('Non-dialogue OCR exclusions need specific frame-backed reasons')
        else:
            raise ValueError('Unreviewed OCR cannot become a Chinese dialogue base')
        mapped.update(output)
    if seen != set(source_cues):
        raise ValueError('Chinese review omits original OCR observations')
    aligned = set()
    for item in report.get('alignment', []):
        cue = output_cues.get(item.get('corrected_cue_id'))
        if (not cue or cue['id'] in aligned or item.get('target_start_ms') != cue['start_ms']
                or item.get('target_end_ms') != cue['end_ms'] or not item.get('evidence')):
            raise ValueError('Chinese review must align every output cue to its actual target times')
        aligned.add(cue['id'])
    if aligned != set(output_cues):
        raise ValueError('Chinese review has unaligned output cues')
    additions = report.get('additional_cues') or []
    added = set()
    for item in additions:
        cue_id = item.get('corrected_cue_id')
        if (cue_id not in output_cues or cue_id in added or not item.get('evidence')
                or not item.get('source_frame_ids') or any(i not in frames for i in item['source_frame_ids'])):
            raise ValueError('OCR-missed additions need source frame evidence')
        added.add(cue_id)
    if mapped | added != set(output_cues):
        raise ValueError('Chinese review contains unsupported output cues')
    whole = report.get('whole_episode_review') or {}
    if (whole.get('status') != 'complete' or whole.get('sampling_gaps_checked') is not True
            or whole.get('missing_chinese_dialogue_count') != 0 or not whole.get('evidence')):
        raise ValueError('Review OCR sampling gaps and whole-episode Chinese coverage before qualification')
    return {'report': ref, 'source': source, 'path': corrected_path, 'target': target,
            'corrected_sha256': digest(corrected_raw), 'cue_count': len(corrected)}


def apply_chinese_review(db, sid, ep, report_path, guard=None):
    from .subtitle_studio import archive_for
    # Do the expensive source-frame verification outside the production DB lock.
    validated = validate_chinese_review(db, sid, ep, report_path)
    with _preparation_gate(db, sid, ep, guard):
        source = validated['source']
        _, raw = _private_file(db.path.parent / 'subtitle-studio', validated['report'], 8 * 1024 * 1024)
        _private_file(db.path.parent / 'subtitle-studio',
                      {'path': source['metadata']['raw_file'], 'sha256': source['raw_hash']})
        from .subtitle_lab import lab_core
        if lab_core().fingerprint(validated['target']['path'], validated['target']['size']) != validated['target']['fingerprint']:
            raise ValueError('Chinese review target changed before registration')
        if digest(Path(validated['path']).read_bytes()) != validated['corrected_sha256']:
            raise ValueError('Corrected Chinese source changed before registration')
        draft_id = archive_for(db).add_base(sid, db.mapping(sid).get('season', 1), ep, validated['path'], {
            'source_key': source['metadata'].get('source_key'), 'track': 'codex-reviewed-' + source['id'],
            'language': 'chi', 'release': source['metadata'].get('release', ''), 'origin': 'codex-reviewed-ocr',
            'media_fingerprint': validated['target']['fingerprint'], 'timing_is_estimated': False,
            'source_draft_id': source['id'], 'source_raw_sha256': source['raw_hash'],
            'chinese_review_report': validated['report'], 'target_matching_status': 'verified'})
    return {'draft_id': draft_id, 'report': validated['report'], 'cue_count': validated['cue_count'],
            'source_preserved': source['id'], 'production_ready_claimed': False}
