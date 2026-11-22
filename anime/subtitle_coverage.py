"""Japanese-first translation coverage. Mechanical evidence, never semantic acceptance.

Read every original source cue AND every rendered non-Chinese event. A Chinese
source-parent count or unrelated simultaneous Chinese cannot close this check.
"""
import hashlib
import json
from pathlib import Path


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _key(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _parse(path):
    from .subtitle_studio import lab_core
    path = Path(path)
    return lab_core().parse_subtitles(path.read_bytes(), path.suffix.lstrip('.'))


def display_events(ass, styles):
    """Deduplicate text shadow/foreground layers; retain different actor IDs."""
    result = {}
    for row in _parse(ass):
        if styles.get(row['style']) not in ('zh', 'source'):
            raise ValueError('Every visible ASS style needs a zh/source coverage role: ' + row['style'])
        event = {k: row[k] for k in ('start_ms', 'end_ms', 'text', 'style', 'actor')}
        identity = _key(event)
        result[identity] = {'id': identity, **event, 'role': styles[row['style']]}
    if not any(e['role'] == 'zh' for e in result.values()):
        raise ValueError('Chinese viewing coverage is empty')
    return list(result.values())


def uncovered(start, end, chinese):
    windows = [(start, end)]
    for row in chinese:
        windows = [part for a, b in windows
                   for part in ((a, min(b, row['start_ms'])), (max(a, row['end_ms']), b))
                   if part[0] < part[1]]
    return windows


def scan(ass, styles, sources):
    events = display_events(ass, styles)
    chinese = [e for e in events if e['role'] == 'zh']
    items = [{'id': 'display:' + e['id'], **{k: e[k] for k in ('start_ms', 'end_ms', 'text')}}
             for e in events if e['role'] == 'source']
    for index, ref in enumerate(sources):
        path = Path(ref['path'])
        if sha(path) != ref['sha256'] or not ref.get('language') or ref['language'] in ('zh', 'chi', 'zho'):
            raise ValueError('Original non-Chinese source missing, changed or mislabelled')
        for row in _parse(path):
            items.append({'id': f'source:{index}:{row["id"]}',
                          **{k: row[k] for k in ('start_ms', 'end_ms', 'text')}})
    for item in items:
        item['uncovered_ms'] = uncovered(item['start_ms'], item['end_ms'], chinese)
        item['overlapping_zh_ids'] = [e['id'] for e in chinese
                                    if e['start_ms'] < item['end_ms'] and e['end_ms'] > item['start_ms']]
    return {'events_sha256': _key(events), 'chinese_events': chinese, 'items': items}


def _private(project, path):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(project).resolve() / 'data/subtitle-studio'):
        raise ValueError('Coverage sources/evidence must remain private')
    return path


def _evidence(project, refs):
    if not refs:
        raise ValueError('Coverage exception needs inspected scene/timing evidence')
    for ref in refs:
        if sha(_private(project, ref['path'])) != ref.get('sha256'):
            raise ValueError('Coverage evidence changed')


def validate(project, path, ass, sid, ep, media_sha, source_ass_sha=None):
    """Require explicit reviewed Chinese correspondence, not guessed translations.

    The owner supplies linguistic decisions; this recomputes exhaustive membership,
    actual output references and time gaps. It cannot judge translation quality.
    """
    if not path:
        raise ValueError('Chinese coverage review required before encoding')
    path = _private(project, path)
    report = json.loads(path.read_text(encoding='utf-8-sig'))
    if (report.get('role') != 'episode_chinese_coverage_review' or report.get('schema') != 1
            or report.get('show_id') != sid or report.get('episode') != ep
            or report.get('media_sha256') != media_sha or not report.get('reviewer')
            or report.get('ass_sha256') != (source_ass_sha or sha(ass))
            or not report.get('source_inventory_evidence')):
        raise ValueError('Chinese coverage review must bind exact episode, source inventory and ASS')
    sources = report.get('sources', [])
    for ref in sources:
        _private(project, ref['path'])
    audit = scan(ass, report['styles'], sources)
    if audit['events_sha256'] != report.get('events_sha256'):
        raise ValueError('Coverage review belongs to different rendered events')
    if any(i['id'].startswith('display:') for i in audit['items']) and not sources:
        raise ValueError('Rendered original-language text needs its archived source inventory')
    decisions = report.get('decisions', [])
    by_id = {d['id']: d for d in decisions}
    if len(by_id) != len(decisions) or set(by_id) != {i['id'] for i in audit['items']}:
        raise ValueError('Every original cue and rendered source event needs a coverage decision')
    chinese = {e['id']: e for e in audit['chinese_events']}
    exceptions = 0
    for item in audit['items']:
        decision = by_id[item['id']]
        if not decision.get('reason') or not decision.get('reviewer'):
            raise ValueError('Each coverage decision needs actual reviewer and semantic basis')
        status = decision.get('status')
        if status in ('nonverbal', 'nonessential_screen_text'):
            if status == 'nonessential_screen_text' and decision.get('kind') != 'sign':
                raise ValueError('Dialogue/lyrics cannot be waived as peripheral screen text')
            _evidence(project, decision.get('evidence_refs'))
            exceptions += 1
            continue
        if status != 'translated' or not decision.get('zh_ids'):
            raise ValueError('Meaningful source dialogue/lyrics/sign text has no Chinese translation: ' + item['id'])
        ids = decision['zh_ids']
        if len(set(ids)) != len(ids) or not set(ids) <= chinese.keys():
            raise ValueError('Coverage references absent Chinese output')
        gaps = uncovered(item['start_ms'], item['end_ms'], [chinese[k] for k in ids])
        # ASS rounding can introduce <=20ms; longer gaps require specific review.
        gaps = [list(g) for g in gaps if g[1] - g[0] > 20]
        if gaps:
            timing = decision.get('timing_review', {})
            if (timing.get('windows_ms') != gaps or not timing.get('reason')
                    or timing.get('status') != 'verified_correspondence'):
                raise ValueError('Mapped Chinese leaves an unreviewed source-only interval: ' + item['id'])
            _evidence(project, timing.get('evidence_refs'))
    return {'path': str(path), 'sha256': sha(path), 'source_cues_and_display_events': len(by_id),
            'reviewed_exceptions': exceptions, 'events_sha256': audit['events_sha256']}


def validate_release(project, root, manifest):
    """Called before new publication and finished acknowledgement, never list/view."""
    if 'chinese-coverage-review.json' not in manifest.get('files', {}):
        raise ValueError('Chinese coverage review missing; player boolean/source counts are insufficient')
    path = Path(root) / 'chinese-coverage-review.json'
    if sha(path) != manifest['files'][path.name]['sha256']:
        raise ValueError('Chinese coverage report changed')
    return validate(project, path, Path(root) / 'bilingual.ass', manifest['show_id'], manifest['episode'],
                    manifest['source_media_sha256'], manifest.get('source_ass_sha256'))


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Generate an UNREVIEWED Japanese-first coverage draft; never translate/waive automatically')
    parser.add_argument('--ass', required=True, type=Path)
    parser.add_argument('--source', action='append', default=[], type=Path, help='Archived target-timed original VTT/SRT/ASS; repeat for songs/sign sources')
    parser.add_argument('--zh-style', action='append', required=True)
    parser.add_argument('--source-style', action='append', default=[])
    parser.add_argument('--show-id', type=int, required=True)
    parser.add_argument('--episode', type=int, required=True)
    parser.add_argument('--media-sha256', required=True)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    styles = {s: 'zh' for s in args.zh_style}
    if set(args.zh_style) & set(args.source_style):
        raise ValueError('A style cannot be both Chinese and original language')
    styles.update({s: 'source' for s in args.source_style})
    sources = [{'path': str(p.resolve()), 'sha256': sha(p), 'language': 'original'} for p in args.source]
    audit = scan(args.ass, styles, sources)
    report = {'schema': 1, 'role': 'episode_chinese_coverage_review', 'show_id': args.show_id,
              'episode': args.episode, 'media_sha256': args.media_sha256, 'ass_sha256': sha(args.ass),
              'events_sha256': audit['events_sha256'], 'styles': styles, 'sources': sources,
              'reviewer': '', 'source_inventory_evidence': '', 'audit': audit,
              'decisions': [{'id': i['id'], 'status': 'pending', 'reviewer': '', 'reason': '',
                             'zh_ids': []} for i in audit['items']]}
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'status': 'unreviewed', 'items': len(audit['items']),
                      'source_only_intervals': sum(bool(i['uncovered_ms']) for i in audit['items'])}))


if __name__ == '__main__':
    main()
