"""Synthetic timelines only: no real subtitle text, media or private paths."""
import contextlib
import io
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


def same_cut(spans, text='x'):
    """Candidate rows copied from master spans without any offset, numbered like an SRT draft."""
    rows = [{'start_ms': s, 'end_ms': e, 'text': text, 'style': '', 'positioned': False, 'id': i + 1, 'valid': True}
            for i, (s, e) in enumerate(spans)]
    return sa.classify(rows, False)


def covered_percent_by_millisecond(target_spans, candidate_spans):
    """Independent control for coverage: mark every target millisecond, count the ones under a candidate."""
    end = max(e for _, e in target_spans + candidate_spans)
    target, candidate = bytearray(end), bytearray(end)
    for s, e in target_spans:
        target[s:e] = b'\x01' * (e - s)
    for s, e in candidate_spans:
        candidate[s:e] = b'\x01' * (e - s)
    total = sum(target)
    return 100 * sum(t & c for t, c in zip(target, candidate)) / total, total


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


def only_cut(result):
    assert len(result['cuts']) == 1
    return result['cuts'][0]


def cue_boundaries_respected(candidate, cut):
    """No range endpoint or representative point falls inside a cue, and overlapping cues stay on one side together."""
    points = cut['position']['candidate_range_ms'] + [cut['candidate_time_ms']]
    assert not any(r['start_ms'] < p < r['end_ms'] for r in candidate for p in points)
    undecided = set(cut['position']['undecided_candidate_ids'])
    for cluster in sa.clusters(candidate):
        ids = set(cluster['ids'])
        assert ids <= undecided or not ids & undecided


def sides_hold(candidate, position, edit):
    by_id = {r['id']: r for r in candidate}
    assert all(by_id[i]['truth_start_ms'] < edit for i in position['supporting_candidate_ids']['before'])
    assert all(by_id[i]['truth_start_ms'] >= edit for i in position['supporting_candidate_ids']['after'])


def test_cut_inside_a_long_cue_free_gap_is_bounded_by_the_whole_gap():
    """Review example: cues cannot locate an edit inside a cue-free stretch, so the stretch itself is the candidate range."""
    master = master_rows()
    edit = 350000
    missing = [r['id'] for r in master if 300000 <= r['start_ms'] and r['end_ms'] <= 385000]
    candidate = candidate_rows(master, lambda t: t + 1000 if t < edit else t + 16000, drop=0, split=0, merge=0, drop_ids=missing)
    result = sa.align(master, candidate)
    cut = only_cut(result)
    position = cut['position']
    gap_start = max(r['end_ms'] for r in candidate if r['truth_start_ms'] < 300000)
    gap_end = min(r['start_ms'] for r in candidate if r['truth_start_ms'] >= 300000)
    assert gap_end - gap_start > 90000 and position['longest_cue_free_gap_ms'] == [gap_start, gap_end]
    low, high = position['candidate_range_ms']
    assert low <= gap_start and high >= gap_end and position['cue_free_ms'] >= gap_end - gap_start
    assert low <= edit + 1000 and edit + 16000 <= high  # the inserted stretch lies inside the range
    assert low <= cut['candidate_time_ms'] <= high and cut['candidate_time_basis'] == sa.CUT_TIME_BASIS
    assert position['bounded_by_evidence'] == {'before': True, 'after': True} and len(position['undecided_candidate_ids']) <= 2
    assert cut['segment_confidence'] == ['high', 'high']
    sides_hold(candidate, position, edit)
    cue_boundaries_respected(candidate, cut)
    point = next(p for p in result['review_points'] if p['kind'] == 'cut')
    assert point['candidate_range_ms'] == [low, high] and point['position_kind'] == position['kind']
    assert point['target_review_range_ms'] == position['target_review_range_ms'] and point['target_review_rows']['count'] > 20
    assert point['reason'] == position['reason'] and 'representative' in point['detail']


def test_cut_among_candidate_only_rows_spans_the_undecided_clusters():
    """Song lines the candidate carries inside the target's lyric block fit either offset; they and the edit stay inside the range."""
    master = master_rows(lyric_block=(300000, 400000))
    edit = 350000
    song = [(318000 + k * 3000, 320200 + k * 3000, f'song{k}') for k in range(26)]  # candidate time, no target dialogue under either offset
    song += [(s + 1200, e + 1200, 'dup') for s, e, _ in song[::4]]  # overlapping pairs
    candidate = candidate_rows(master, lambda t: t + 1000 if t < edit else t + 16000, drop=0, split=0, merge=0, extra=song)
    result = sa.align(master, candidate)
    cut = only_cut(result)
    position = cut['position']
    assert position['kind'] == 'undecided_cues' and result['summary']['cut_position_kinds'] == {'single_gap': 0, 'undecided_cues': 1}
    core = {r['id'] for r in candidate if 'truth_start_ms' not in r}
    assert len(core) == 33 and core <= set(position['undecided_candidate_ids'])
    low, high = position['candidate_range_ms']
    assert low <= min(r['start_ms'] for r in candidate if r['id'] in core) and high >= max(r['end_ms'] for r in candidate if r['id'] in core)
    assert low <= edit + 1000 and edit + 16000 <= high and low <= cut['candidate_time_ms'] <= high
    offsets = [s['offset_ms'] for s in result['segments']]
    assert position['target_review_range_ms'] == [low + min(offsets), high + max(offsets)]
    assert position['cue_free_ms'] < high - low  # the undecided clusters take time inside the range
    sides_hold(candidate, position, edit)
    cue_boundaries_respected(candidate, cut)
    assert result['lyrics']['OP_JP']['candidate_rows_inside_block'] == 33


def test_cut_between_well_supported_cues_stays_within_a_few_cues():
    """With supported cues on both sides the range is the inserted stretch plus at most a couple of adjacent cues."""
    master = master_rows()
    a, b = next((a, b) for a, b in zip(master, master[1:]) if a['end_ms'] >= 600000)
    edit = (a['end_ms'] + b['start_ms']) // 2
    candidate = candidate_rows(master, lambda t: t + 1000 if t < edit else t + 16000)
    result = sa.align(master, candidate)
    cut = only_cut(result)
    position = cut['position']
    low, high = position['candidate_range_ms']
    assert low <= edit + 1000 and edit + 16000 <= high and low <= cut['candidate_time_ms'] <= high
    last_before = max(r['id'] for r in candidate if r.get('truth_start_ms', edit) < edit)
    first_after = min(r['id'] for r in candidate if r.get('truth_start_ms', -1) >= edit)
    assert min(position['supporting_candidate_ids']['before']) >= last_before - 2
    assert max(position['supporting_candidate_ids']['after']) <= first_after + 2
    assert len(position['undecided_candidate_ids']) <= 2 and position['bounded_by_evidence'] == {'before': True, 'after': True}
    assert position['longest_cue_free_gap_ms'][1] - position['longest_cue_free_gap_ms'][0] >= 15000
    sides_hold(candidate, position, edit)
    cue_boundaries_respected(candidate, cut)


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
    control = result['control_rows']
    assert len(control) == 4 and all(candidate[u['id'] - 1]['role'] == 'control' for u in control)
    assert not any(u['overlapping_target_ids'] for u in control)
    assert not {u['id'] for u in control} & {u['id'] for u in result['unreferenced_candidate']}
    assert [o['role'] for o in result['outside_target']] == ['control']  # the pre-roll music mark maps before 0
    assert result['summary']['control_rows'] == 4 and result['summary']['candidate_rows'] == len(candidate)


def test_coverage_counts_each_target_millisecond_once():
    """Review finding: summed candidate durations let duplicates compensate for missing dialogue."""
    master = master_rows(count=80)
    spans = [(r['start_ms'], r['end_ms']) for r in master]
    first = spans[:40]
    share, total = covered_percent_by_millisecond(spans, first)
    assert 40 < share < 60
    for name, candidate in (
            ('duplicate', first * 2),
            ('nested', first + [(s + (e - s) // 4, e - (e - s) // 4) for s, e in first]),
            ('partial', [(s, s + (e - s) * 3 // 5) for s, e in first] + [(s + (e - s) * 2 // 5, e) for s, e in first])):
        summary = sa.align(master, same_cut(candidate))['summary']
        assert summary['target_time_covered_percent'] == pytest.approx(share, abs=0.01), name
        assert summary['target_dialogue_time_ms'] == total
        assert summary['target_time_covered_ms'] == round(share * total / 100)
    assert summary['target_time_covered_basis'] == sa.COVERAGE_BASIS


def test_coverage_denominator_is_the_union_of_overlapping_target_rows():
    master = master_rows(count=80)
    second = [{**r, 'start_ms': r['start_ms'] + (r['end_ms'] - r['start_ms']) // 2, 'end_ms': r['end_ms'] + 300,
               'style': 'Top_CN'} for r in master[::3]]  # a second speaker overlapping every third row
    for i, row in enumerate(second):
        row['id'] = 1000 + i
    target = sa.classify(master + second, True)
    spans = [(r['start_ms'], r['end_ms']) for r in target]
    candidate = [(r['start_ms'], r['end_ms']) for r in master[:40]]
    share, total = covered_percent_by_millisecond(spans, candidate)
    summary = sa.align(target, same_cut(candidate))['summary']
    assert summary['target_dialogue_time_ms'] == total < sum(e - s for s, e in spans)
    assert summary['target_time_covered_percent'] == pytest.approx(share, abs=0.01)


def test_control_marker_does_not_fill_a_dialogue_gap():
    """Review finding: a pure music mark in place of a spoken row paired as high-confidence dialogue."""
    master = master_rows(count=80)
    candidate = same_cut([(r['start_ms'], r['end_ms']) for r in master])
    candidate[39]['text'] = '\u266a'
    candidate[40]['text'] = '（物音）'
    sa.classify(candidate, False)
    assert [row['role'] for row in candidate[39:41]] == ['control', 'control']
    result = sa.align(master, candidate)
    assert {40, 41} <= {row['id'] for row in result['target_without_candidate']}
    assert result['summary']['target_without_candidate'] == 2
    assert result['summary']['target_time_covered_percent'] < 100
    assert not any(p['candidate_id'] in (40, 41) for p in result['pairs'])
    control = {row['id']: row for row in result['control_rows']}
    assert control[40]['overlapping_target_ids'] == [40] and control[41]['overlapping_target_ids'] == [41]
    assert result['summary']['control_rows'] == 2 and result['summary']['candidate_rows'] == 80


def direct_profile(timeline, units, window_ms, step_ms):
    """Independent control: intersect every shifted unit with every target interval at each grid offset."""
    return [sum(max(0, min(u['e'] + d, b) - max(u['s'] + d, a)) for u in units for a, b in timeline.intervals)
            for d in range(-window_ms, window_ms + 1, step_ms)]


def test_offset_profile_equals_direct_interval_intersection():
    """Review finding: the sweep started from zero overlap and emitted each grid value one step late."""
    assert sa.offset_profile(sa.Timeline([(0, 1000)]), [{'s': 0, 'e': 1000}], 100, 100) == [900, 1000, 900]
    assert sa.offset_profile(sa.Timeline([(50, 1000)]), [{'s': 0, 'e': 1000}], 100, 100) == [850, 950, 900]
    rng = random.Random(3)
    for _ in range(40):
        intervals, t = [], rng.randint(0, 500)
        for _ in range(rng.randint(1, 12)):
            t += rng.randint(0, 900)
            length = rng.randint(1, 1500)
            intervals.append((t, t + length))
            t += length
        timeline = sa.Timeline(intervals)
        units = [{'s': s, 'e': s + rng.randint(1, 1500)} for s in (rng.randint(-2000, t + 2000) for _ in range(rng.randint(1, 6)))]
        window, step = rng.choice([(700, 20), (1000, 30), (1500, 7), (3000, 100), (90, 13)])
        assert sa.offset_profile(timeline, units, window, step) == direct_profile(timeline, units, window, step)


def test_cli_never_overwrites_inputs_or_an_existing_output(tmp_path, monkeypatch):
    """Review finding: --out equal to --target replaced the source subtitle with the proposal."""
    master = master_rows(count=60)
    candidate = candidate_rows(master, lambda t: t + 1000)
    target, cand = tmp_path / 'target.json', tmp_path / 'candidate.json'
    target.write_text(json.dumps(master), encoding='utf-8')
    cand.write_text(json.dumps(candidate), encoding='utf-8')
    before = (target.read_bytes(), cand.read_bytes())
    alias = tmp_path / 'alias.json'
    alias.symlink_to(target)
    monkeypatch.chdir(tmp_path)
    base = ['--target', str(target), '--candidate', 'candidate.json']
    for out in (str(target), 'target.json', str(alias), 'alias.json', str(tmp_path / 'missing' / '..' / 'candidate.json')):
        for extra in ([], ['--overwrite']):
            with pytest.raises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
                sa.main(base + ['--out', out] + extra)
            assert refused.value.code == 2
    assert (target.read_bytes(), cand.read_bytes()) == before
    assert not (tmp_path / 'missing').exists()
    out = tmp_path / 'proposal.json'
    with contextlib.redirect_stdout(io.StringIO()):
        sa.main(base + ['--out', str(out)])
    first = out.read_bytes()
    with pytest.raises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
        sa.main(base + ['--out', str(out)])
    assert out.read_bytes() == first
    with contextlib.redirect_stdout(io.StringIO()):
        sa.main(base + ['--out', str(out), '--overwrite'])
    assert json.loads(out.read_text(encoding='utf-8'))['schema'] == sa.SCHEMA
    assert (target.read_bytes(), cand.read_bytes()) == before


def test_json_ids_are_kept_or_rejected_and_ass_srt_numbering_is_unchanged():
    """Review finding: explicit JSON ids [7, 19] came back as [1, 2], breaking evidence references."""
    def cue(i, **extra):
        return {'start_ms': 1000 * i, 'end_ms': 1000 * i + 800, 'text': f'line{i}', **extra}
    parse = lambda rows: sa.parse_cues(json.dumps(rows).encode(), 'json')
    assert [r['id'] for r in parse({'cues': [cue(1, id=7), cue(2, id=19)]})] == [7, 19]
    assert [r['id'] for r in parse([cue(1, id='C12'), cue(2, id='C12.1'), cue(3, id='C13', text='')])] == ['C12', 'C12.1']
    assert [r['id'] for r in parse([cue(1), cue(2, id=None)])] == [1, 2]
    for layout in ([cue(1, id=7), cue(2, id=7)], [cue(1, id=7), cue(2)], [cue(1, id=None), cue(2, id=3)],
                   [cue(1, id=1.5)], [cue(1, id=True)], [cue(1, id='')]):
        with pytest.raises(ValueError):
            parse(layout)
    srt = b'10\n00:00:01,000 --> 00:00:02,000\na\n\n20\n00:00:03,000 --> 00:00:04,000\nb\n'
    assert [r['id'] for r in sa.parse_cues(srt, 'srt')] == [1, 2]
    ass = ('[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
           'Dialogue: 0,0:00:01.00,0:00:02.00,Text_CN,,0,0,0,,a\nDialogue: 0,0:00:03.00,0:00:04.00,Text_CN,,0,0,0,,\n'
           'Dialogue: 0,0:00:05.00,0:00:06.00,Text_CN,,0,0,0,,c\n').encode()
    assert [r['id'] for r in sa.parse_cues(ass, 'ass')] == [1, 2]
    master = master_rows(count=60)
    target = sa.classify(parse([{**cue(0), **{k: r[k] for k in ('start_ms', 'end_ms', 'style')}, 'id': f'C{r["id"]}'} for r in master]), True)
    candidate = sa.classify(parse([{**cue(0), 'start_ms': r['start_ms'] + 1000, 'end_ms': r['end_ms'] + 1000, 'id': 500 + r['id']}
                                   for r in master]), False)
    result = sa.align(target, candidate)
    assert {p['primary_target_id'] for p in result['pairs']} <= {f'C{r["id"]}' for r in master}
    assert all(p['candidate_id'] > 500 for p in result['pairs']) and json.dumps(result)


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


def write_fixture(tmp_path, master, candidate):
    ass = ['[Script Info]', 'ScriptType: v4.00+', '', '[Events]',
           'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text']
    ass += [f"Dialogue: 0,{ass_time(r['start_ms'])},{ass_time(r['end_ms'])},{r['style']},,0,0,0,,{r['text']}" for r in master]
    ass.append('Dialogue: 0,0:00:00.00,0:00:00.00,Text_CN,,0,0,0,,invalid timing row')
    (tmp_path / 'base.ass').write_text('\n'.join(ass) + '\n', encoding='utf-8')
    srt = [f"{r['id']}\n{srt_time(r['start_ms'])} --> {srt_time(r['end_ms'])}\n{r['text']}\n" for r in candidate]
    (tmp_path / 'cand.srt').write_text('\n'.join(srt), encoding='utf-8-sig')


def test_cli_writes_proposal_in_lab_schema(tmp_path):
    master = master_rows(count=120, lyric_block=(35000, 80000))
    candidate = candidate_rows(master, lambda t: t + 1000)
    write_fixture(tmp_path, master, candidate)
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


def test_proposal_extraction_and_check_flow(tmp_path):
    """The handoff steps from docs/SUBTITLE-ALIGNMENT-HANDOFF.md: extract the proposal, check cue placement, list review points."""
    master = master_rows()
    edit = 400000
    candidate = candidate_rows(master, lambda t: t + 1000 if t < edit else t + 9000, extra=[(300, 900, 'pre')])
    write_fixture(tmp_path, master, candidate)
    out = tmp_path / 'result.json'
    with contextlib.redirect_stdout(io.StringIO()):
        sa.main(['--target', str(tmp_path / 'base.ass'), '--candidate', str(tmp_path / 'cand.srt'), '--out', str(out), '--draft-id', 'DRAFT'])
    result = json.loads(out.read_text(encoding='utf-8'))
    alignments = result['proposal']
    assert list(alignments) == ['DRAFT'] and alignments['DRAFT']['status'] == 'proposed'
    assert alignments['DRAFT']['target_fingerprint'] is None and alignments['DRAFT']['reviewer'] is None
    assert 'SECRET' not in out.read_text(encoding='utf-8')
    rows = sa.load(tmp_path / 'cand.srt', False)
    segments = alignments['DRAFT']['segments']
    unplaced = [r['id'] for r in rows if r['valid'] and sa.mapped(segments, r['start_ms'], r['end_ms']) is None]
    assert unplaced == [o['id'] for o in result['outside_target']] == [1]
    checklist = [p for p in result['review_points'] if p['kind'] == 'cut']
    assert len(checklist) == 1 and checklist[0]['candidate_range_ms'][0] <= checklist[0]['candidate_time_ms'] <= checklist[0]['candidate_range_ms'][1]
    assert checklist[0]['target_review_rows']['count'] >= 1 and result['summary']['cut_position_kinds']['single_gap'] + result['summary']['cut_position_kinds']['undecided_cues'] == 1


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
