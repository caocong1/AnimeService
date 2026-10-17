"""Synthetic timelines only: no real subtitle text, media or private paths."""
import json
import random

import pytest

from anime import subtitle_alignment as sa

PAL = 25 / 23.976


def master_rows(seed=1, count=300, lyric_block=None):
    rng = random.Random(seed)
    rows, t = [], 2000
    for i in range(count):
        if lyric_block and t + 4700 > lyric_block[0] and t < lyric_block[1] + 500:
            t = lyric_block[1] + 500  # no dialogue near the song block, like an OP without speech
        dur = rng.randint(700, 4200)
        rows.append({'start_ms': t, 'end_ms': t + dur, 'text': f'SECRET{i}', 'style': 'Text_CN', 'positioned': False})
        t += dur + rng.choice([80, 120, 200, 400, 900, 1500, 3000])
    if lyric_block:
        k = lyric_block[0]
        while k + 2500 <= lyric_block[1]:
            rows.append({'start_ms': k, 'end_ms': k + 2500, 'text': 'LYRIC', 'style': 'OP_JP', 'positioned': False})
            k += 2600
    rows.sort(key=lambda r: r['start_ms'])
    for i, row in enumerate(rows):
        row.update(id=i + 1, valid=True)
    return sa.classify(rows, True)


def candidate_rows(master, transform, seed=2, drop=0.04, split=0.04, merge=0.04, jitter=120, drop_ids=(), extra=()):
    """Candidate in its own cut's time; transform maps master time to candidate time."""
    rng = random.Random(seed)
    dialogue = [r for r in master if r['role'] == 'dialogue' and r['id'] not in set(drop_ids)]
    rows, i = [], 0
    while i < len(dialogue):
        row, roll = dialogue[i], rng.random()
        if roll < drop:
            i += 1
            continue
        nxt = dialogue[i + 1] if i + 1 < len(dialogue) else None
        same_cut = nxt and transform(nxt['end_ms']) - transform(row['start_ms']) == nxt['end_ms'] - row['start_ms']
        if roll < drop + merge and same_cut:
            pieces, text = [(row['start_ms'], nxt['end_ms'])], row['text']
            i += 2
        elif roll < drop + merge + split and row['end_ms'] - row['start_ms'] > 1800:
            mid = (row['start_ms'] + row['end_ms']) // 2
            pieces, text = [(row['start_ms'], mid - 50), (mid + 50, row['end_ms'])], row['text']
            i += 1
        else:
            pieces, text = [(row['start_ms'], row['end_ms'])], row['text']
            i += 1
        for s, e in pieces:
            cs, ce = transform(s) + rng.randint(-jitter, jitter), transform(e) + rng.randint(-jitter, jitter)
            rows.append({'start_ms': cs, 'end_ms': max(ce, cs + 300), 'text': text, 'style': '', 'positioned': False,
                         'truth_start_ms': s})
    for s, e, text in extra:
        rows.append({'start_ms': s, 'end_ms': e, 'text': text, 'style': '', 'positioned': False})
    rows.sort(key=lambda r: (r['start_ms'], r['end_ms']))
    for i, row in enumerate(rows):
        row.update(id=i + 1, valid=True)
    return sa.classify(rows, False)


def lab_map(segments, row):
    """Mirror of the lab core's mapped_cue validation: ints, forward spans, cue entirely inside one segment."""
    for seg in segments:
        a, b, c, d = (seg[k] for k in ('source_start_ms', 'source_end_ms', 'target_start_ms', 'target_end_ms'))
        assert all(type(n) is int for n in (a, b, c, d)) and b > a and d > c and min(a, c) >= 0
        if a <= row['start_ms'] and row['end_ms'] <= b:
            slope = (d - c) / (b - a)
            return round(c + (row['start_ms'] - a) * slope)
    raise ValueError('cue crosses edit or lacks reviewed alignment segment')


def mapping_errors(result, candidate):
    segments = result['proposal']['DRAFT']['segments'] if 'proposal' in result else result['segments']
    outside = {o['id'] for o in result['outside_target']}
    errors = []
    for row in candidate:
        if 'truth_start_ms' in row and row['id'] not in outside:
            errors.append(abs(lab_map(segments, row) - row['truth_start_ms']))
    return errors


def test_constant_offset_gives_one_high_confidence_segment():
    master = master_rows()
    candidate = candidate_rows(master, lambda t: t + 1000)
    result = sa.align(master, candidate)
    assert len(result['segments']) == 1 and result['cuts'] == []
    assert abs(result['segments'][0]['offset_ms'] + 1000) <= 60
    assert result['segments'][0]['confidence'] == 'high'
    assert max(mapping_errors(result, candidate)) <= 200
    assert result['summary']['target_time_covered_percent'] >= 85
    assert result['summary']['pairs']['one_to_one'] > 0.8 * len(result['pairs'])
    assert not result['summary']['drift_suspected']


def test_edit_in_candidate_cut_becomes_two_segments_between_clusters():
    master = master_rows()
    a, b = next((a, b) for a, b in zip(master, master[1:]) if a['end_ms'] >= 600000)
    edit = (a['end_ms'] + b['start_ms']) // 2  # fifteen extra seconds in the candidate cut inside a dialogue gap
    candidate = candidate_rows(master, lambda t: t + 1000 if t < edit else t + 16000)
    result = sa.align(master, candidate)
    assert [round(s['offset_ms'], -2) for s in result['segments']] == [-1000, -16000]
    cut = result['cuts'][0]
    before = max(r['start_ms'] for r in candidate if r.get('truth_start_ms', 10**9) < edit)
    after = min(r['start_ms'] for r in candidate if r.get('truth_start_ms', -1) >= edit)
    assert before < cut['candidate_time_ms'] < after
    assert cut['offset_change_ms'] == pytest.approx(-15000, abs=100)
    assert max(mapping_errors(result, candidate)) <= 200
    assert any(p['kind'] == 'cut' for p in result['review_points'])


def test_candidate_rows_before_target_start_are_outside_and_spans_stay_non_negative():
    master = master_rows()
    candidate = candidate_rows(master, lambda t: t + 5000, extra=[(200, 1800, 'pre'), (2000, 3500, 'roll')])
    result = sa.align(master, candidate)
    assert {o['id'] for o in result['outside_target']} == {1, 2}
    assert result['segments'][0]['target_start_ms'] >= 0
    assert max(mapping_errors(result, candidate)) <= 200


def test_frame_rate_drift_is_detected_as_a_scale():
    master = master_rows()
    candidate = candidate_rows(master, lambda t: round(t * PAL) + 500, jitter=60)
    result = sa.align(master, candidate)
    assert result['summary']['drift_suspected']
    assert result['summary']['scale'] == pytest.approx(1 / PAL, abs=0.0005)
    assert max(mapping_errors(result, candidate)) <= 250
    assert any(p['kind'] == 'drift' for p in result['review_points'])


def test_lyric_block_is_excluded_from_structure_and_reported():
    master = master_rows(lyric_block=(35000, 115000))
    candidate = candidate_rows(master, lambda t: t + 1000)
    result = sa.align(master, candidate)
    assert len(result['segments']) == 1
    lyrics = result['lyrics']['OP_JP']
    assert lyrics['rows'] > 20 and lyrics['rows_overlapped_by_candidate'] == 0
    assert not any(m['style'] == 'OP_JP' for m in result['target_without_candidate'])
    assert max(mapping_errors(result, candidate)) <= 200


def test_song_rows_transcribed_only_in_candidate_do_not_create_segments():
    """An ASR draft carries OP lines the Chinese base keeps as lyric rows; they must not be mapped onto later dialogue."""
    master = master_rows(lyric_block=(35000, 115000))
    song = [(36000 + k * 3000, 38500 + k * 3000, f'song{k}') for k in range(25)]
    candidate = candidate_rows(master, lambda t: t + 1000, extra=song)
    result = sa.align(master, candidate)
    assert len(result['segments']) == 1 and abs(result['segments'][0]['offset_ms'] + 1000) <= 60
    assert result['lyrics']['OP_JP']['candidate_rows_inside_block'] == 25
    assert [run['count'] for run in result['candidate_extra_runs']] == [25]
    assert max(mapping_errors(result, candidate)) <= 200


def test_missing_target_lines_and_extra_candidate_content_are_listed_as_runs():
    master = master_rows()
    last = max(r['end_ms'] for r in master)
    extra = [(last + 6000 + k * 3000, last + 8000 + k * 3000, f'extra{k}') for k in range(8)]
    candidate = candidate_rows(master, lambda t: t + 1000, drop=0, drop_ids=range(101, 107), extra=extra)
    result = sa.align(master, candidate)
    assert any(set(range(101, 107)) <= set(run['ids']) for run in result['target_missing_runs'])
    assert [run['count'] for run in result['candidate_extra_runs']] == [8]
    kinds = {p['kind'] for p in result['review_points']}
    assert {'target_missing_run', 'candidate_extra_run'} <= kinds


def test_repeated_lines_are_paired_by_time_and_text_differences_counted():
    master = master_rows()
    candidate = candidate_rows(master, lambda t: t + 1000, drop=0, split=0, merge=0)
    for row in candidate[10:12]:
        row['text'] = 'またね、また明日'
    for row in candidate[20:30]:
        row['text'] = 'XYZXYZXYZXYZ'
    result = sa.align(master, candidate, compare_text=True)
    comparison = result['text_comparison']
    assert comparison['duplicate_text_groups'] == 2 and comparison['duplicate_text_pairing_conflicts'] == 0
    targets = [p['primary_target_id'] for p in result['pairs'] if p['candidate_id'] in (11, 12)]
    assert len(targets) == 2 and targets[0] != targets[1]
    assert len(comparison['low_similarity_pairs']) >= 8
    assert comparison['buckets']['>=0.8'] >= comparison['compared_pairs'] - 12


def test_control_only_rows_are_not_reported_as_unreferenced_dialogue():
    master = master_rows()
    gaps = [(a['end_ms'] + 1600, a['end_ms'] + 2400, '（笑い声）')
            for a, b in zip(master, master[1:]) if b['start_ms'] - a['end_ms'] >= 2900][:4]
    candidate = candidate_rows(master, lambda t: t + 1000, extra=gaps + [(1000, 1900, '♪～')])
    result = sa.align(master, candidate)
    control = [u for u in result['unreferenced_candidate'] if u['kind'] == 'control_only']
    assert control and all(candidate[u['id'] - 1]['role'] == 'control' for u in control)
    assert result['summary']['candidate_rows'] == len(candidate)


def test_output_contains_no_subtitle_text_unless_requested(tmp_path):
    master = master_rows()
    candidate = candidate_rows(master, lambda t: t + 1000)
    result = sa.align(master, candidate, compare_text=True)
    dumped = json.dumps(result, ensure_ascii=False)
    assert 'SECRET' not in dumped and 'LYRIC' not in dumped


def ass_time(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, frac = divmod(rem, 1000)
    return f'{h}:{m:02d}:{s:02d}.{frac // 10:02d}'


def srt_time(ms):
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, frac = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d},{frac:03d}'


def test_cli_writes_proposal_in_lab_schema(tmp_path):
    master = master_rows(count=120, lyric_block=(35000, 80000))
    candidate = candidate_rows(master, lambda t: t + 1000)
    ass = ['[Script Info]', 'ScriptType: v4.00+', '', '[Events]',
           'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    ass += [f"Dialogue: 0,{ass_time(r['start_ms'])},{ass_time(r['end_ms'])},{r['style']},,0,0,0,,{r['text']}" for r in master]
    ass.append('Dialogue: 0,0:00:00.00,0:00:00.00,Text_CN,,0,0,0,,invalid timing row')
    (tmp_path / 'base.ass').write_text('\n'.join(ass) + '\n', encoding='utf-8')
    srt = [f"{r['id']}\n{srt_time(r['start_ms'])} --> {srt_time(r['end_ms'])}\n{r['text']}\n" for r in candidate]
    (tmp_path / 'cand.srt').write_text('\n'.join(srt), encoding='utf-8-sig')
    out = tmp_path / 'proposal.json'
    sa.main(['--target', str(tmp_path / 'base.ass'), '--candidate', str(tmp_path / 'cand.srt'), '--out', str(out),
             '--draft-id', 'DRAFT', '--text'])
    result = json.loads(out.read_text(encoding='utf-8'))
    assert result['schema'] == sa.SCHEMA and result['caveats']
    assert result['inputs']['target']['roles']['lyrics'] > 0 and result['inputs']['target']['file'] == 'base.ass'
    assert result['inputs']['target']['sha256'] and len(result['inputs']['candidate']['sha256']) == 64
    draft = result['proposal']['DRAFT']
    assert draft['status'] == 'proposed' and draft['reviewer'] is None and draft['target_fingerprint'] is None
    assert set(draft['segments'][0]) == {'source_start_ms', 'source_end_ms', 'target_start_ms', 'target_end_ms'}
    parsed = sa.load(tmp_path / 'cand.srt', False)
    assert max(mapping_errors({'segments': draft['segments'], 'outside_target': result['outside_target']},
                              [{**r, 'truth_start_ms': c['truth_start_ms']} for r, c in zip(parsed, candidate)
                               if 'truth_start_ms' in c])) <= 200
    assert 'SECRET' not in out.read_text(encoding='utf-8')


def test_parse_formats_and_invalid_rows():
    ass = ('[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
           'Dialogue: 0,0:00:01.00,0:00:02.50,Text_CN,,0,0,0,,{\\pos(10,10)}a,b\n'
           'Dialogue: 0,0:00:03.00,0:00:02.50,Text_CN,,0,0,0,,backwards\n'
           'Dialogue: 0,0:00:04.00,0:00:05.00,OP_JP,,0,0,0,,\n'
           'Dialogue: 0,0:00:06.00,0:00:07.00,Screen,,0,0,0,,sign\n').encode('utf-8-sig')
    rows = sa.classify(sa.parse_cues(ass, 'ass'), True)
    assert [(r['id'], r['valid'], r['role'], r['text']) for r in rows] == [
        (1, True, 'sign', 'a,b'), (2, False, 'dialogue', 'backwards'), (3, True, 'sign', 'sign')]
    explicit = sa.classify(sa.parse_cues(ass.replace(b'Screen', b'OP_JP'), 'ass'), True, dialogue_styles={'Other'})
    assert [r['role'] for r in explicit] == ['sign', 'other', 'lyrics']
    vtt = b'WEBVTT\n\n00:01.000 --> 00:02.000 position:10%\n(laugh)\n\n00:00:03,000 --> 00:00:04,000\n<i>hello</i>\n'
    rows = sa.classify(sa.parse_cues(vtt, 'vtt'), False)
    assert [(r['start_ms'], r['role'], r['text'], r['positioned']) for r in rows] == [
        (1000, 'control', '(laugh)', True), (3000, 'dialogue', 'hello', False)]
    rows = sa.parse_cues(json.dumps([{'start_ms': 5, 'end_ms': 9, 'text': 'x'}]).encode(), 'json')
    assert rows[0]['id'] == 1 and rows[0]['valid']
    rows = sa.parse_cues(json.dumps({'cues': [{'start': 1.5, 'end': 2.0, 'text': 'y'}]}).encode(), 'json')
    assert (rows[0]['start_ms'], rows[0]['end_ms']) == (1500, 2000)
    with pytest.raises(ValueError):
        sa.parse_cues(b'', 'txt')
