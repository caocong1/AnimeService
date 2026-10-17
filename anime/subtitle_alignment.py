"""Language-agnostic piecewise timing alignment between two subtitle timelines.

The target is the exact-media cue list (usually the archived Chinese base) and the
candidate is a subtitle for another cut of the same episode: platform CC, fansub or
local ASR draft. Only cue intervals are compared, never translated text, so Chinese
wording can never be force-aligned to Japanese speech. The output is a proposal in the
lab's ``alignments`` schema with ``status=proposed``: a reviewer must inspect every
reported cut, drift and low-confidence segment against actual picture and audio before
changing it to ``accepted``. Temporal overlap is a pairing candidate, not acceptance.

Coverage is the share of the target's dialogue time (the union of its valid dialogue
intervals) that lies under candidate dialogue rows paired by overlap after mapping.
Duplicate, nested and overlapping rows count once and archived control rows (music marks,
bracketed sound cues) never count, so coverage can only fall when dialogue is missing.
"""
import argparse
import bisect
import difflib
import hashlib
import html
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 'anime-subtitle-alignment-proposal/v2'
REVISION = 'interval-overlap-dp-v2'
# Frame-rate conversions; a 25 fps broadcast candidate drifts against 23.976 fps media.
SCALES = (1.0, 25 / 23.976, 23.976 / 25, 24 / 23.976, 23.976 / 24, 25 / 24, 24 / 25)
LYRIC_STYLE = re.compile(r'(?:^|[^a-z])(?:op|ed|opening|ending|song|lyrics?|karaoke|insert|ost|chorus)(?:[^a-z]|$)', re.I)
SIGN_STYLE = re.compile(r'screen|sign|title|staff|comment|note|credit|logo|typeset|caption', re.I)
CONTROL_TEXT = re.compile(r'^\s*(?:[（(\[【][^）)\]】]*[）)\]】]\s*)+$|^[\s♪♬～〜…・]+$')
SPEAKER_TAG = re.compile(r'^\s*[（(\[【][^）)\]】]*[）)\]】]')
CAVEATS = [
    'Interval alignment only: no text was translated, listened to or force-aligned.',
    'A segment boundary is a hypothesis about an edit; inspect picture/audio at every cut before accepting.',
    'Pairs are temporal overlap candidates; speaker, wording and phrase boundaries still need review.',
    'status=proposed cannot enter an authored timeline; the reviewer sets accepted with evidence.',
    'Coverage is target dialogue time under paired candidate rows; it proves neither wording, source independence nor audio acceptance.',
]
COVERAGE_BASIS = ('union of valid target dialogue intervals intersected with the union of paired candidate '
                  'dialogue rows after mapping, over the target union; control rows never count')


def milliseconds(value):
    parts = value.strip().replace(',', '.').split(':')
    if len(parts) == 2:
        parts.insert(0, '0')
    if len(parts) != 3:
        raise ValueError('invalid timestamp')
    number = (int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])) * 1000
    if not math.isfinite(number):
        raise ValueError('invalid timestamp')
    return round(number)


def plaintext(text):
    return html.unescape(re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]*\}', '', text))
                         .replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ')).strip()


def parse_cues(data, extension):
    """Same text/time subset as the lab archive; ids follow ``number_rows``; invalid timing rows stay out of alignment."""
    text = data.decode('utf-8-sig')
    rows = []
    ext = extension.lower().lstrip('.')
    if ext in ('ass', 'ssa'):
        fields = None
        in_events = False
        for line in text.splitlines():
            if line.startswith('['):
                in_events = line.strip().lower() == '[events]'
                continue
            if not in_events:
                continue
            low = line.lower()
            if low.startswith('format:'):
                fields = [s.strip().lower() for s in line.split(':', 1)[1].split(',')]
            elif low.startswith('dialogue:'):
                if not fields or 'text' not in fields:
                    raise ValueError('ASS events missing Format')
                values = line.split(':', 1)[1].lstrip().split(',', len(fields) - 1)
                if len(values) != len(fields):
                    raise ValueError('invalid ASS event')
                event = dict(zip(fields, values))
                rows.append({'start_ms': milliseconds(event['start']), 'end_ms': milliseconds(event['end']),
                             'text': plaintext(event['text']), 'style': event.get('style', '').strip(),
                             'positioned': bool(re.search(r'\\(?:pos|move|clip|iclip)\(', event['text']))})
    elif ext in ('vtt', 'srt'):
        for block in re.split(r'\r?\n\s*\r?\n', text):
            if block.lstrip().startswith(('WEBVTT', 'NOTE', 'STYLE', 'REGION')):
                continue
            match = re.search(r'((?:\d+:)?\d+:\d+[.,]\d+)\s*-->\s*((?:\d+:)?\d+:\d+[.,]\d+)[^\n]*\n?(.*)', block, re.S)
            if match:
                rows.append({'start_ms': milliseconds(match[1]), 'end_ms': milliseconds(match[2]),
                             'text': plaintext(match[3]), 'style': '',
                             'positioned': bool(re.search(r'-->[^\n]*(?:position:|line:|align:)', block))})
    elif ext == 'json':
        source = json.loads(text)
        if isinstance(source, dict):  # a lab draft archive keeps its rows under 'cues'
            source = source.get('cues', [])
        for row in source:  # parsed rows in milliseconds, or the lab's OCR/ASR rows in seconds
            start = int(row['start_ms']) if 'start_ms' in row else round(float(row['start']) * 1000)
            end = int(row['end_ms']) if 'end_ms' in row else round(float(row['end']) * 1000)
            rows.append({'start_ms': start, 'end_ms': end, 'text': str(row.get('text', '')),
                         'style': str(row.get('style', '')), 'positioned': bool(row.get('positioned')),
                         'id': row.get('id')})
    else:
        raise ValueError('supported formats: UTF-8 ASS/SSA/SRT/VTT/parsed JSON')
    rows = [r for r in rows if r['text']]
    if not rows:
        raise ValueError('empty subtitles')
    number_rows(rows)
    for row in rows:
        row['valid'] = row['start_ms'] >= 0 and row['end_ms'] > row['start_ms']
    return rows


def number_rows(rows):
    """Archive numbering (1-based over text rows, like the lab parser) unless every JSON row carries its own id.

    Explicit ids are evidence references and are kept verbatim. A layout that mixes rows with and
    without ids, repeats an id, or uses anything but integers and non-empty strings is rejected
    rather than silently renumbered.
    """
    ids = [row.get('id') for row in rows]
    if all(i is None for i in ids):
        for i, row in enumerate(rows):
            row['id'] = i + 1
    elif any(i is None for i in ids):
        raise ValueError('cue ids must be present on every row or on none')
    elif any(isinstance(i, bool) or not isinstance(i, (int, str)) or i == '' for i in ids):
        raise ValueError('cue ids must be integers or non-empty strings')
    elif len(set(ids)) != len(ids):
        raise ValueError('duplicate cue ids')


def classify(rows, by_style, dialogue_styles=None, exclude_styles=None):
    for row in rows:
        style = row['style']
        if CONTROL_TEXT.match(row['text']):
            role = 'control'
        elif not by_style:
            role = 'dialogue'
        elif dialogue_styles is not None and style in dialogue_styles:
            role = 'dialogue'
        elif exclude_styles and style in exclude_styles:
            role = 'other'
        elif LYRIC_STYLE.search(style):
            role = 'lyrics'
        elif SIGN_STYLE.search(style) or row['positioned']:
            role = 'sign'
        else:
            role = 'other' if dialogue_styles is not None else 'dialogue'
        row['role'] = role
    return rows


def clusters(rows):
    """Maximal groups of overlapping rows; an edit boundary can only be placed between clusters."""
    out = []
    for row in sorted(rows, key=lambda r: (r['start_ms'], r['end_ms'])):
        if out and row['start_ms'] < out[-1]['end_ms']:
            out[-1]['end_ms'] = max(out[-1]['end_ms'], row['end_ms'])
            out[-1]['ids'].append(row['id'])
        else:
            out.append({'start_ms': row['start_ms'], 'end_ms': row['end_ms'], 'ids': [row['id']]})
    return out


class Timeline:
    def __init__(self, intervals):
        self.intervals = sorted(intervals)
        self.starts = [a for a, _ in self.intervals]
        self.ends = sorted(b for _, b in self.intervals)
        self.max_len = max((b - a for a, b in self.intervals), default=0)

    def overlap(self, s, e):
        lo = bisect.bisect_left(self.starts, s - self.max_len)
        hi = bisect.bisect_right(self.starts, e)
        return sum(max(0, min(e, b) - max(s, a)) for a, b in self.intervals[lo:hi])

    def near(self, values, x, tol):
        i = bisect.bisect_left(values, x - tol)
        return i < len(values) and values[i] <= x + tol


def unit_score(timeline, unit, offset, tol):
    s, e = unit['s'] + offset, unit['e'] + offset
    ratio = min(1.0, timeline.overlap(s, e) / (e - s))
    hits = timeline.near(timeline.starts, s, tol) + timeline.near(timeline.ends, e, tol)
    return 0.6 * ratio + 0.2 * hits, ratio, hits


def offset_profile(timeline, units, window_ms, step_ms):
    """Total overlap as a function of a constant offset, exact at every grid point ``k * step_ms - window_ms``.

    The overlap of one unit with one target interval is a trapezoid in the offset, i.e. a sum of
    hinges ``sign * max(0, d - b)``. A hinge whose break ``b`` lies before grid point ``j`` is worth
    ``sign * (d_j - b)`` there and ``sign * step_ms`` more at each later point, so breaks need not sit
    on the grid and overlap already present at the left edge of the window is counted.
    """
    size = 2 * window_ms // step_ms + 1
    slope = [0] * size
    base = [0] * size
    for unit in units:
        s1, e1 = unit['s'], unit['e']
        lo = bisect.bisect_left(timeline.starts, s1 - window_ms - timeline.max_len)
        hi = bisect.bisect_right(timeline.starts, e1 + window_ms)
        for t1, t2 in timeline.intervals[lo:hi]:
            d1, d2 = sorted((t1 - s1, t2 - e1))
            for b, sign in ((t1 - e1, 1), (d1, -1), (d2, -1), (t2 - s1, 1)):
                j = max(0, -((-(b + window_ms)) // step_ms))  # first grid point at or after the break
                if j < size:
                    slope[j] += sign
                    base[j] += sign * (j * step_ms - window_ms - b)
    profile = []
    rate = value = 0
    for k in range(size):
        value += rate * step_ms + base[k]
        rate += slope[k]
        profile.append(value)
    return profile


def peaks(profile, window_ms, step_ms, count, separation_ms):
    chosen = []
    for k in sorted(range(len(profile)), key=lambda k: -profile[k]):
        d = k * step_ms - window_ms
        if all(abs(d - c) >= separation_ms for c in chosen):
            chosen.append(d)
            if len(chosen) == count:
                break
    return chosen


def candidate_offsets(timeline, units, window_ms, step_ms, span=24, stride=8):
    found = peaks(offset_profile(timeline, units, window_ms, step_ms), window_ms, step_ms, 6, 400)
    for i in range(0, max(1, len(units) - span + 1), stride):
        found += peaks(offset_profile(timeline, units[i:i + span], window_ms, step_ms), window_ms, step_ms, 3, 400)
    merged = []
    for d in sorted(found):
        if not merged or d - merged[-1] > 100:
            merged.append(d)
    return merged


def assign_offsets(scores, penalty):
    """Each unit takes one offset; changing offset between neighbours costs ``penalty`` (an edit)."""
    best = [-x for x in scores[0]]
    back = []
    for row in scores[1:]:
        jmin = min(range(len(best)), key=best.__getitem__)
        switch = best[jmin] + penalty
        step = []
        nxt = []
        for j, score in enumerate(row):
            if best[j] <= switch:
                nxt.append(best[j] - score)
                step.append(j)
            else:
                nxt.append(switch - score)
                step.append(jmin)
        back.append(step)
        best = nxt
    j = min(range(len(best)), key=best.__getitem__)
    path = [j]
    for step in reversed(back):
        j = step[j]
        path.append(j)
    return path[::-1]


def refine(timeline, units, offset, tol, radius=600, step=10):
    return max(range(offset - radius, offset + radius + 1, step),
               key=lambda d: sum(unit_score(timeline, u, d, tol)[0] for u in units))


def choose_scale(timeline, units, window_ms, step_ms):
    """Prefer the frame-rate ratio whose best constant offset explains clearly more overlap than chance.

    Dense dialogue overlaps something at almost any offset, and the exact profile carries that
    floor, so each peak is measured above the profile's median before the 15% margin is applied.
    """
    results = {}
    for scale in SCALES:
        scaled = [{'s': round(u['start_ms'] * scale), 'e': round(u['end_ms'] * scale)} for u in units]
        profile = offset_profile(timeline, scaled, window_ms, step_ms)
        results[scale] = max(profile) - sorted(profile)[len(profile) // 2]
    chosen = max(results, key=lambda s: (results[s], s == 1.0))
    if results[chosen] < 1.15 * results[1.0]:
        chosen = 1.0
    return chosen, {f'{s:.5f}': v for s, v in results.items()}


def runs_from_path(path, offsets):
    runs = []
    for i, j in enumerate(path):
        if runs and runs[-1]['offset_index'] == j:
            runs[-1]['last'] = i
        else:
            runs.append({'offset_index': j, 'offset': offsets[j], 'first': i, 'last': i})
    return runs


def merge_runs(runs, tolerance):
    merged = []
    for run in runs:
        if merged and abs(run['offset'] - merged[-1]['offset']) <= tolerance:
            merged[-1]['last'] = run['last']
        else:
            merged.append(dict(run))
    return merged


def chance_level(timeline, units, window_ms, tol, samples=16):
    """Mean unit score and boundary-hit rate at evenly spaced wrong offsets: dense dialogue overlaps something almost anywhere."""
    offsets = [round(-window_ms + (k + 0.5) * 2 * window_ms / samples) for k in range(samples)]
    score = hits = 0
    for u in units:
        for d in offsets:
            s, _, h = unit_score(timeline, u, d, tol)
            score += s
            hits += h
    count = len(units) * samples
    return {'score': score / count, 'hit_rate': hits / (2 * count)}


def evidence(timeline, units, offset, tol, chance):
    """Support above chance and how many standard deviations the boundary hits exceed a random offset."""
    total = hits = 0
    for u in units:
        s, _, h = unit_score(timeline, u, offset, tol)
        total += max(0.0, s - chance['score'])
        hits += h
    trials = 2 * len(units)
    spread = math.sqrt(trials * chance['hit_rate'] * (1 - chance['hit_rate'])) or 1.0
    return {'support': round(total, 2), 'hits': hits, 'hit_rate': round(hits / trials, 3),
            'hit_z': round((hits - trials * chance['hit_rate']) / spread, 2)}


def null_evidence(timeline, units, offset, tol, window_ms, chance, samples=24):
    """Best evidence the same units reach at wrong offsets with the same local refinement: the selection-biased null."""
    best_support = best_hits = 0
    for k in range(samples):
        d = round(-window_ms + (k + 0.5) * 2 * window_ms / samples)
        if abs(d - offset) < 3000:
            continue
        found = evidence(timeline, units, refine(timeline, units, d, tol, step=50), tol, chance)
        best_support, best_hits = max(best_support, found['support']), max(best_hits, found['hits'])
    return best_support, best_hits


def weak_run(timeline, units, run, tol, window_ms, chance, penalty):
    found = evidence(timeline, units, run['offset'], tol, chance)
    if found['support'] < penalty or found['hit_z'] < 3:
        return True
    if found['hit_z'] >= 12:
        return False
    null_support, null_hits = null_evidence(timeline, units, run['offset'], tol, window_ms, chance)
    return found['support'] < null_support + penalty or found['hits'] <= null_hits


def prune_runs(timeline, units, runs, tol, window_ms, chance, penalty):
    """A run that chance explains as well, or that is not worth an edit, joins the neighbour explaining it best."""
    while len(runs) > 1:
        weak = next((k for k, run in enumerate(runs)
                     if weak_run(timeline, units[run['first']:run['last'] + 1], run, tol, window_ms, chance, penalty)), None)
        if weak is None:
            return runs
        members = units[runs[weak]['first']:runs[weak]['last'] + 1]
        neighbours = [runs[j] for j in (weak - 1, weak + 1) if 0 <= j < len(runs)]
        runs[weak]['offset'] = max(neighbours, key=lambda n: evidence(timeline, members, n['offset'], tol, chance)['support'])['offset']
        runs = merge_runs(runs, 120)
    return runs


def align_units(timeline, units, scale, window_ms, step_ms, tol, penalty):
    for u in units:
        u['s'], u['e'] = round(u['start_ms'] * scale), round(u['end_ms'] * scale)
    offsets = candidate_offsets(timeline, units, window_ms, step_ms)
    chance = chance_level(timeline, units, window_ms, tol)
    scores = [[max(0.0, unit_score(timeline, u, d, tol)[0] - chance['score']) for d in offsets] for u in units]
    runs = runs_from_path(assign_offsets(scores, penalty), offsets)
    for _ in range(2):
        for run in runs:
            run['offset'] = refine(timeline, units[run['first']:run['last'] + 1], run['offset'], tol)
        runs = prune_runs(timeline, units, merge_runs(runs, 120), tol, window_ms, chance, penalty)
    for run in runs:
        run.update(evidence(timeline, units[run['first']:run['last'] + 1], run['offset'], tol, chance))
    return runs, chance


def segment_spans(units, runs, scale):
    """Source spans end at cluster gap midpoints so no candidate cue can cross a segment."""
    segments = []
    for k, run in enumerate(runs):
        first, last = units[run['first']], units[run['last']]
        start = 0 if k == 0 else (units[run['first'] - 1]['end_ms'] + first['start_ms']) // 2
        end = last['end_ms'] + 1000 if k == len(runs) - 1 else (last['end_ms'] + units[run['last'] + 1]['start_ms']) // 2
        if round(start * scale + run['offset']) < 0:
            start = math.ceil(-run['offset'] / scale) + 1
        segments.append({'index': k, 'source_start_ms': start, 'source_end_ms': end,
                         'target_start_ms': round(start * scale + run['offset']),
                         'target_end_ms': round(end * scale + run['offset']), 'offset_ms': run['offset']})
    return segments


def mapped(segments, start_ms, end_ms):
    """Same interpolation rule as the lab core: a cue must lie entirely inside one segment."""
    for index, seg in enumerate(segments):
        a, b, c, d = seg['source_start_ms'], seg['source_end_ms'], seg['target_start_ms'], seg['target_end_ms']
        if a <= start_ms and end_ms <= b:
            slope = (d - c) / (b - a)
            return round(c + (start_ms - a) * slope), round(c + (end_ms - a) * slope), index
    return None


def union(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def intersection_ms(first, second):
    total = i = j = 0
    while i < len(first) and j < len(second):
        total += max(0, min(first[i][1], second[j][1]) - max(first[i][0], second[j][0]))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return total


def coverage(target, pairs):
    """Covered and total target dialogue time, both over interval unions so no millisecond counts twice."""
    target_union = union((r['start_ms'], r['end_ms']) for r in target)
    covered = intersection_ms(target_union, union((p['mapped_start_ms'], p['mapped_end_ms']) for p in pairs))
    return covered, sum(b - a for a, b in target_union)


def nearest_gap(rows, starts, start_ms, end_ms):
    i = bisect.bisect_left(starts, start_ms)
    best = None
    for other in rows[max(0, i - 2):i + 2]:
        gap = max(other['start_ms'] - end_ms, start_ms - other['end_ms'], 0)
        if best is None or gap < best[1]:
            best = (other['id'], gap)
    return best


def normalized_text(text):
    text = SPEAKER_TAG.sub('', unicodedata.normalize('NFKC', text))
    return ''.join(ch for ch in text.lower() if not unicodedata.category(ch)[0] in 'PZS')


def consecutive_runs(ids_in_order, flagged, minimum):
    runs, current = [], []
    for row in ids_in_order:
        if flagged(row):
            current.append(row)
        else:
            if len(current) >= minimum:
                runs.append(current)
            current = []
    if len(current) >= minimum:
        runs.append(current)
    return runs


def align(target_rows, candidate_rows, window_ms=150000, step_ms=20, tol=200, penalty=2.5, compare_text=False):
    target = [r for r in target_rows if r['valid'] and r['role'] == 'dialogue']
    usable = [r for r in candidate_rows if r['valid']]
    structural = [r for r in usable if r['role'] != 'control']
    if not target or not structural:
        raise ValueError('target dialogue rows and candidate rows are both required')
    timeline = Timeline([(r['start_ms'], r['end_ms']) for r in target])
    units = clusters(structural)
    scale, sweep = choose_scale(timeline, units, window_ms, step_ms)
    runs, chance = align_units(timeline, units, scale, window_ms, step_ms, tol, penalty)
    segments = segment_spans(units, runs, scale)
    by_id = {r['id']: r for r in candidate_rows}
    for seg, run in zip(segments, runs):
        anchored = 0
        for unit in units[run['first']:run['last'] + 1]:
            _, ratio, hits = unit_score(timeline, unit, run['offset'], tol)
            unit['anchored'] = ratio >= 0.5 or (ratio >= 0.25 and hits >= 1)
            anchored += unit['anchored']
        count = run['last'] - run['first'] + 1
        seg.update(units=count, anchored_units=anchored, support=run['support'], hit_rate=run['hit_rate'], hit_z=run['hit_z'],
                   confidence='high' if run['hit_z'] >= 6 and anchored >= 8 and anchored >= 0.7 * count else
                   'medium' if run['hit_z'] >= 3 and anchored >= 4 else 'low',
                   candidate_ids=[units[run['first']]['ids'][0], units[run['last']]['ids'][-1]])
    cuts = [{'between_segments': [a['index'], b['index']], 'candidate_time_ms': a['source_end_ms'],
             'target_before_ms': a['target_end_ms'], 'target_after_ms': b['target_start_ms'],
             'offset_change_ms': b['offset_ms'] - a['offset_ms']} for a, b in zip(segments, segments[1:])]
    # Pairing by temporal overlap after mapping; archived control rows are listed, never paired.
    target_partners = {r['id']: [] for r in target}
    pairs, unreferenced, outside, control = [], [], [], []
    target_sorted = sorted(target, key=lambda r: r['start_ms'])
    target_starts = [r['start_ms'] for r in target_sorted]
    for row in usable:
        place = mapped(segments, row['start_ms'], row['end_ms'])
        if place is None:
            outside.append({'id': row['id'], 'start_ms': row['start_ms'], 'end_ms': row['end_ms'], 'role': row['role']})
            continue
        ms, me, index = place
        lo = bisect.bisect_left(target_starts, ms - timeline.max_len)
        hi = bisect.bisect_right(target_starts, me)
        partners = []
        for other in target_sorted[lo:hi]:
            overlap = min(me, other['end_ms']) - max(ms, other['start_ms'])
            if overlap >= max(120, 0.2 * min(me - ms, other['end_ms'] - other['start_ms'])):
                partners.append((other, overlap))
        if row['role'] == 'control':  # temporal overlap only: a music mark or sound cue is not dialogue support
            control.append({'id': row['id'], 'mapped_start_ms': ms, 'mapped_end_ms': me, 'segment': index,
                            'overlapping_target_ids': [o['id'] for o, _ in partners]})
            continue
        if not partners:
            entry = {'id': row['id'], 'mapped_start_ms': ms, 'mapped_end_ms': me, 'segment': index}
            near = nearest_gap(target_sorted, target_starts, ms, me)
            if near and near[1] <= 600:  # a hint for review, never a pairing claim
                entry.update(nearest_target_id=near[0], gap_ms=near[1])
            unreferenced.append(entry)
            continue
        for other, overlap in partners:
            target_partners[other['id']].append((row['id'], overlap))
        best = max(partners, key=lambda p: p[1])
        pairs.append({'candidate_id': row['id'], 'target_ids': [o['id'] for o, _ in partners],
                      'primary_target_id': best[0]['id'], 'segment': index,
                      'mapped_start_ms': ms, 'mapped_end_ms': me, 'overlap_ms': best[1],
                      'candidate_ratio': round(best[1] / (me - ms), 3),
                      'target_ratio': round(best[1] / (best[0]['end_ms'] - best[0]['start_ms']), 3),
                      'relation': 'one_to_many' if len(partners) > 1 else 'one_to_one'})
    for pair in pairs:
        if pair['relation'] == 'one_to_one' and len(target_partners[pair['target_ids'][0]]) > 1:
            pair['relation'] = 'many_to_one'
        strength = min(pair['candidate_ratio'], pair['target_ratio'])
        pair['confidence'] = 'high' if strength >= 0.7 else 'medium' if strength >= 0.4 else 'low'
    missing = [{'id': r['id'], 'start_ms': r['start_ms'], 'end_ms': r['end_ms'], 'style': r['style']}
               for r in target_sorted if not target_partners[r['id']]]
    missing_ids = {m['id'] for m in missing}
    missing_runs = [{'target_start_ms': run[0]['start_ms'], 'target_end_ms': run[-1]['end_ms'], 'count': len(run),
                     'ids': [r['id'] for r in run]}
                    for run in consecutive_runs(target_sorted, lambda r: r['id'] in missing_ids, 3)]
    extra_ids = {u['id'] for u in unreferenced}
    extra_runs = [{'candidate_start_ms': run[0]['start_ms'], 'candidate_end_ms': run[-1]['end_ms'], 'count': len(run),
                   'ids': [r['id'] for r in run]}
                  for run in consecutive_runs(sorted(usable, key=lambda r: r['start_ms']), lambda r: r['id'] in extra_ids, 3)]
    covered, total = coverage(target, pairs)
    lyrics = lyric_report(target_rows, usable, segments)
    summary = {'scale': round(scale, 6), 'drift_suspected': scale != 1.0, 'segments': len(segments), 'cuts': len(cuts),
               'target_dialogue_rows': len(target), 'candidate_rows': len(usable),
               'candidate_rows_invalid_timing': sum(1 for r in candidate_rows if not r['valid']),
               'pairs': {k: sum(1 for p in pairs if p['relation'] == k) for k in ('one_to_one', 'one_to_many', 'many_to_one')},
               'pair_confidence': {k: sum(1 for p in pairs if p['confidence'] == k) for k in ('high', 'medium', 'low')},
               'unreferenced_candidate': len(unreferenced), 'control_rows': len(control),
               'outside_target': len(outside), 'target_without_candidate': len(missing),
               'target_dialogue_time_ms': total, 'target_time_covered_ms': covered,
               'target_time_covered_percent': round(100 * covered / total, 2) if total else 0,
               'target_time_covered_basis': COVERAGE_BASIS}
    result = {'schema': SCHEMA, 'revision': REVISION, 'parameters': {'window_ms': window_ms, 'step_ms': step_ms,
              'boundary_tolerance_ms': tol, 'switch_penalty': penalty, 'chance_score': round(chance['score'], 4),
              'chance_hit_rate': round(chance['hit_rate'], 4), 'scale_sweep_peak_above_median_ms': sweep},
              'summary': summary, 'segments': segments, 'cuts': cuts, 'pairs': pairs,
              'unreferenced_candidate': unreferenced, 'control_rows': control, 'outside_target': outside,
              'target_without_candidate': missing, 'target_missing_runs': missing_runs,
              'candidate_extra_runs': extra_runs, 'lyrics': lyrics}
    if compare_text:
        result['text_comparison'] = text_comparison(pairs, by_id, {r['id']: r for r in target})
    result['review_points'] = review_points(result)
    return result


def lyric_report(target_rows, candidate_rows, segments):
    """Does the candidate carry timed rows inside the target's lyric blocks? Candidates with music marks are listed."""
    report = {}
    mapped_rows = []
    for row in candidate_rows:
        place = mapped(segments, row['start_ms'], row['end_ms'])
        if place:
            mapped_rows.append((place[0], place[1], '♪' in row['text'] or '♬' in row['text'], row['id']))
    for style in sorted({r['style'] for r in target_rows if r['role'] == 'lyrics'}):
        rows = [r for r in target_rows if r['style'] == style and r['valid']]
        block = (min(r['start_ms'] for r in rows), max(r['end_ms'] for r in rows))
        inside = [m for m in mapped_rows if m[1] > block[0] and m[0] < block[1]]
        overlapped = sum(1 for r in rows if any(m[1] > r['start_ms'] and m[0] < r['end_ms'] for m in inside))
        report[style] = {'rows': len(rows), 'block_ms': list(block), 'rows_overlapped_by_candidate': overlapped,
                         'candidate_rows_inside_block': len(inside),
                         'candidate_rows_with_music_mark_inside_block': sum(1 for m in inside if m[2]),
                         'candidate_ids_inside_block': [m[3] for m in inside]}
    report['candidate_rows_with_music_mark'] = sum(1 for m in mapped_rows if m[2])
    return report


def text_comparison(pairs, candidate_by_id, target_by_id):
    """Optional cross-draft check when both sides are the same language; repeated lines are paired by time first."""
    similarities = []
    for pair in pairs:
        if pair['relation'] != 'one_to_one':
            continue
        a = normalized_text(candidate_by_id[pair['candidate_id']]['text'])
        b = normalized_text(target_by_id[pair['target_ids'][0]]['text'])
        if a and b:
            similarities.append((pair['candidate_id'], pair['target_ids'][0], round(difflib.SequenceMatcher(None, a, b).ratio(), 3)))
    groups = {}
    for row in candidate_by_id.values():
        key = normalized_text(row['text'])
        if len(key) >= 4:
            groups.setdefault(key, []).append(row['id'])
    duplicates = [ids for ids in groups.values() if len(ids) > 1]
    paired_target = {}
    conflicts = 0
    for pair in pairs:
        for t in pair['target_ids']:
            paired_target.setdefault(t, []).append(pair['candidate_id'])
    for ids in duplicates:
        for targets in paired_target.values():
            if len(set(targets) & set(ids)) > 1:
                conflicts += 1
    buckets = {'>=0.8': 0, '0.6-0.8': 0, '<0.6': 0}
    for _, _, s in similarities:
        buckets['>=0.8' if s >= 0.8 else '0.6-0.8' if s >= 0.6 else '<0.6'] += 1
    return {'compared_pairs': len(similarities), 'buckets': buckets,
            'mean_similarity': round(sum(s for _, _, s in similarities) / len(similarities), 3) if similarities else None,
            'low_similarity_pairs': [[c, t, s] for c, t, s in similarities if s < 0.6],
            'duplicate_text_groups': len(duplicates), 'duplicate_text_rows': sum(len(g) for g in duplicates),
            'duplicate_text_pairing_conflicts': conflicts}


def review_points(result):
    points = []
    if result['summary']['drift_suspected']:
        points.append({'kind': 'drift', 'detail': f"frame-rate scale {result['summary']['scale']} chosen; verify first/last anchors"})
    for cut in result['cuts']:
        points.append({'kind': 'cut', 'candidate_time_ms': cut['candidate_time_ms'],
                       'target_before_ms': cut['target_before_ms'], 'target_after_ms': cut['target_after_ms'],
                       'offset_change_ms': cut['offset_change_ms'],
                       'detail': 'inspect picture/audio on both sides of this candidate time'})
    for seg in result['segments']:
        if seg['confidence'] != 'high':
            points.append({'kind': 'segment_confidence', 'segment': seg['index'], 'confidence': seg['confidence'],
                           'anchored_units': seg['anchored_units'], 'units': seg['units']})
    for run in result['target_missing_runs']:
        points.append({'kind': 'target_missing_run', **run, 'detail': 'target dialogue without candidate rows: missing source lines or an edit'})
    for run in result['candidate_extra_runs']:
        points.append({'kind': 'candidate_extra_run', **run, 'detail': 'candidate rows without target dialogue: extra content, lyrics or signs'})
    if result['summary']['pairs']['one_to_many']:
        points.append({'kind': 'splits', 'count': result['summary']['pairs']['one_to_many'],
                       'detail': 'candidate rows spanning several target cues need auditable split plans'})
    if result['outside_target']:
        points.append({'kind': 'outside_target', 'count': len(result['outside_target'])})
    return points


def proposal(result, draft_id):
    return {draft_id: {'target_fingerprint': None, 'status': 'proposed', 'reviewer': None, 'evidence': None,
                       'tool': f'{SCHEMA} {REVISION}',
                       'segments': [{k: seg[k] for k in ('source_start_ms', 'source_end_ms', 'target_start_ms', 'target_end_ms')}
                                    for seg in result['segments']]}}


def describe(path, rows):
    data = Path(path).read_bytes()
    styles = {}
    for row in rows:
        styles.setdefault(row['style'] or '-', {'rows': 0, 'role': row['role']})['rows'] += 1
    return {'file': Path(path).name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'rows': len(rows),
            'styles': styles, 'roles': {k: sum(1 for r in rows if r['role'] == k) for k in ('dialogue', 'lyrics', 'sign', 'control', 'other')}}


def load(path, by_style, dialogue_styles=None, exclude_styles=None):
    name = Path(path).name.lower()
    extension = 'json' if name.endswith('.json') else Path(path).suffix
    return classify(parse_cues(Path(path).read_bytes(), extension), by_style, dialogue_styles, exclude_styles)


def run(target_path, candidate_path, draft_id='CANDIDATE_DRAFT_ID', dialogue_styles=None, exclude_styles=None,
        window_ms=150000, compare_text=False, include_text=False):
    target_rows = load(target_path, True, dialogue_styles, exclude_styles)
    candidate_rows = load(candidate_path, False)
    result = align(target_rows, candidate_rows, window_ms=window_ms, compare_text=compare_text)
    result['generated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    result['inputs'] = {'target': describe(target_path, target_rows), 'candidate': describe(candidate_path, candidate_rows)}
    result['proposal'] = proposal(result, draft_id)
    result['caveats'] = CAVEATS
    if include_text:
        text = {r['id']: r['text'] for r in candidate_rows}
        for pair in result['pairs']:
            pair['candidate_text'] = text[pair['candidate_id']]
    return result


def output_path(out, inputs, overwrite=False):
    """Never write over an input (aliases and symlinks included); an existing output needs ``overwrite``."""
    path = Path(out)
    for source in inputs:
        source = Path(source)
        if path.resolve() == source.resolve() or (path.exists() and source.exists() and path.samefile(source)):
            raise FileExistsError(f'--out must not be an input file: {out}')
    if path.exists() and not overwrite:
        raise FileExistsError(f'--out already exists; choose a new path or pass --overwrite: {out}')
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True, help='exact-media cue list (ASS/SRT/VTT/parsed JSON)')
    parser.add_argument('--candidate', required=True, help='subtitle from another cut of the same episode')
    parser.add_argument('--out', required=True, help='proposal JSON to write (contains no subtitle text by default)')
    parser.add_argument('--overwrite', action='store_true', help='replace an existing output file; inputs are never replaced')
    parser.add_argument('--draft-id', default='CANDIDATE_DRAFT_ID', help='lab draft id to key the alignments proposal')
    parser.add_argument('--dialogue-styles', help='comma separated target styles that are ordinary dialogue')
    parser.add_argument('--exclude-styles', help='comma separated target styles to leave out of alignment')
    parser.add_argument('--window-ms', type=int, default=150000, help='largest offset searched between the two cuts')
    parser.add_argument('--text', action='store_true', help='also compare paired texts (same-language drafts only)')
    parser.add_argument('--include-text', action='store_true', help='copy candidate text into pairs (private output only)')
    args = parser.parse_args(argv)
    try:
        out = output_path(args.out, (args.target, args.candidate), args.overwrite)
    except FileExistsError as exc:
        parser.error(str(exc))
    split = lambda value: set(s.strip() for s in value.split(',') if s.strip()) if value else None
    result = run(args.target, args.candidate, args.draft_id, split(args.dialogue_styles), split(args.exclude_styles),
                 args.window_ms, args.text, args.include_text)
    with out.open('w' if args.overwrite else 'x', encoding='utf-8') as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
