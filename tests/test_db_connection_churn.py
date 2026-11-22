"""Read passes reuse one read snapshot; outputs equal the per-lookup implementation."""
import contextlib
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_reliability import setup
from test_subtitle_delivery import delivery
from test_subtitle_studio import ready
from anime.engine import GATE_SETTINGS
from anime.fidelity import fidelity_blocker
from anime.native_indexes import candidate_mapping
from anime.releases import Rejected, validate_title
from anime.source_review import family
from anime.subtitle_studio import SubtitleStudio, register_subtitle_studio
from anime.viewing_workflow import queue_snapshot


@pytest.fixture
def connects(monkeypatch):
    opened = []
    original = sqlite3.connect
    def counting(*args, **kwargs):
        opened.append(str(args[0]))
        return original(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', counting)
    @contextlib.contextmanager
    def counted():
        start = len(opened)
        result = SimpleNamespace(count=0)
        yield result
        result.count = len(opened) - start
    return counted


def old_candidates(studio, sid):
    """The previous per-lookup implementation, kept verbatim as the oracle."""
    out = []; mapping = studio.db.mapping(sid)
    for c in studio.db.rows('''SELECT c.* FROM candidates c JOIN sources s ON s.id=c.source_id
           WHERE c.show_id=? AND s.enabled=1 ORDER BY c.id''', (sid,)):
        try:
            eps = validate_title(c['title'], candidate_mapping(mapping, c), {}, False)
            if len(eps) != 1: continue
            studio.ensure(sid, eps[0])
            if fidelity_blocker(studio.db, c['title']): continue
        except (ValueError, Rejected): continue
        out.append({**c, 'episode': eps[0], 'family': family(c['title'])})
    return out


def add_candidates(setup, first, last):
    for i in range(first, last):
        ep = 1 + i % 15  # episodes 13-15 fall outside the confirmed mapping
        group = ('Skymoon-Raws' if i % 7 == 3 else 'Group')
        upscale = ' AI 4K' if i % 5 == 4 else ''
        setup[3](f'[{group}] Example S01E{ep:02d}{upscale} [1080p][CHS]', url=f'https://mikanani.me/{i}.torrent')


def test_candidates_equal_old_lookup_loop_with_constant_connections(setup, connects):
    db, q, e, r, _ = ready(setup)
    db.set('source_publisher_blacklist', [{'publisher': 'Skymoon', 'aliases': ['Skymoon-Raws'], 'reason': 'fixture'}])
    db.set('source_fidelity', 'original')
    studio = e.subtitle_studio
    costs = []
    for total in (5, 40):
        add_candidates(setup, 0 if total == 5 else 5, total)
        expected = old_candidates(studio, 42)
        with connects() as cost:
            assert studio.candidates(42) == expected
        costs.append(cost.count)
    assert {c['episode'] for c in expected} <= set(range(1, 13)) and len(expected) > 5
    assert costs == [1, 1]
    db.execute("UPDATE shows SET state='dropped' WHERE id=42")
    assert studio.candidates(42) == old_candidates(studio, 42) == []


def test_media_gate_pairs_match_per_episode_ensure_in_one_snapshot(setup, connects):
    db, q, e, r, _ = ready(setup)
    studio = e.subtitle_studio
    pairs = [(42, ep) for ep in range(0, 15)] + [(999, 1)]
    def passes(sid, ep):
        try:
            studio.ensure(sid, ep)
            return True
        except ValueError:
            return False
    expected = {pair for pair in pairs if passes(*pair)}
    with connects() as cost:
        assert studio.permitted(pairs) == expected
    assert cost.count == 1 and expected == {(42, ep) for ep in range(1, 13)}
    db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    assert studio.permitted(pairs) == set()


def test_media_skips_episodes_outside_the_gate_like_ensure(setup, monkeypatch, tmp_path):
    db, q, e, r, _ = ready(setup)
    monkeypatch.setattr('anime.subtitle_studio.lab_core', lambda: SimpleNamespace(
        fingerprint=lambda path, size=None: 'fp-' + str(path), digest=lambda raw: hashlib.sha256(raw).hexdigest(),
        canonical=lambda value: json.dumps(value, sort_keys=True)))
    for ep in (2, 3, 13):
        path = tmp_path / f'ep{ep}.mkv'
        path.write_bytes(b'x' * ep)
        db.execute("INSERT INTO episodes(show_id,episode,hash,path,status) VALUES(42,?,'',?,'complete')", (ep, str(path)))
    calls = []
    original = SubtitleStudio.ensure
    def ensure(self, sid, ep, submission=False):
        calls.append((sid, ep))
        return original(self, sid, ep, submission)
    monkeypatch.setattr(SubtitleStudio, 'ensure', ensure)
    assert [item['episode'] for item in e.subtitle_studio.media()] == [2, 3]
    assert sorted(calls) == [(42, 2), (42, 3), (42, 13)]


def test_viewing_workflow_reads_one_snapshot_with_the_same_body(setup, connects):
    db, q, e, r, _ = ready(setup)
    for ep in (1, 2, 3):
        db.execute("INSERT INTO subtitle_studio_jobs VALUES(42,?,'waiting_review',?,'',0)",
                   (ep, json.dumps({'draft_ids': [], 'target': None})))
    app = FastAPI()
    register_subtitle_studio(app, SimpleNamespace(subtitle_studio=e.subtitle_studio, source_review=e.source_review))
    expected = json.loads(json.dumps(queue_snapshot(db)))
    client = TestClient(app)
    with connects() as cost:
        body = client.get('/api/viewing-workflow').json()
    assert body == expected and cost.count == 1
    assert [job['episode'] for show in body['queues']['watching'] for job in show['jobs']] == [1, 2, 3]


def test_discover_connections_do_not_grow_with_candidates(setup, monkeypatch, connects):
    db, q, e, r, _ = ready(setup)
    db.set('source_publisher_blacklist', [{'publisher': 'Skymoon', 'aliases': ['Skymoon-Raws'], 'reason': 'fixture'}])
    archive = SimpleNamespace(register=lambda sid, season, ep: f'{sid}/{season}/{ep}',
                              connect=lambda: contextlib.nullcontext(SimpleNamespace(execute=lambda *a: None)))
    monkeypatch.setattr('anime.subtitle_studio.archive_for', lambda db: archive)
    monkeypatch.setattr('anime.subtitle_studio.lab_core', lambda: SimpleNamespace(
        canonical=lambda value: json.dumps(value, sort_keys=True), digest=lambda raw: hashlib.sha256(raw).hexdigest()))
    studio = e.subtitle_studio
    calls = []
    monkeypatch.setattr(studio, 'refresh_job', lambda sid, ep, archive=None: calls.append(('job', ep)))
    monkeypatch.setattr(studio, 'queue', lambda c, role='clean_base': calls.append(('queue', c['episode'], c['title'])))
    monkeypatch.setattr(e.source_review, 'start', lambda sid, ids: calls.append(('start', tuple(ids))))
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    costs = []
    for group in ('Late', 'Late'):
        for _ in range(10):
            setup[3](f'[{group}] Example S01E02 [1080p][CHS]', url=f'https://mikanani.me/{group}-{len(costs)}-{_}.torrent')
        calls.clear()
        with connects() as cost:
            studio.discover()
        costs.append((cost.count, list(calls)))
    # Ten more same-family candidates change neither the cost nor the decisions.
    assert costs[0] == costs[1] and costs[0][0] < 60
    assert costs[0][1] == [('job', 2), ('queue', 2, '[Late] Example S01E02 [1080p][CHS]')]


def test_completed_sources_checks_selection_before_db_backed_gates(tmp_path, monkeypatch):
    from anime.db import Store
    import anime.subtitle_chinese as chinese
    db = Store(tmp_path / 'data' / 'test.db')
    for i, selected in enumerate(({'1': {'name': 'a.mkv', 'size': 1}}, {'2': {'name': 'b.mkv', 'size': 1}}, {})):
        db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path) VALUES(?,42,?,'complete',?,?)",
                   (f'h{i}', f'title {i}', json.dumps({'files': [], 'ids': [], 'selected': selected}), str(tmp_path)))
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path) VALUES('h9',42,'odd','complete','[]',?)", (str(tmp_path),))
    checked = []
    monkeypatch.setattr(chinese, 'fidelity_blocker', lambda db, title, selection=None: checked.append(title) or 'blocked')
    assert chinese.completed_sources(db, 42, 2) == []
    # A malformed selection still reaches the original gates in the original order.
    assert checked == ['title 1', 'odd']


def test_gate_key_and_recheck_due_equal_per_lookup_reads_in_one_snapshot(setup, connects):
    db, q, e, c = setup
    def old_gate_key(t):
        sid = t['show_id']; policy = db.one('SELECT config FROM source_policies WHERE show_id=?', (sid,))
        parts = {k: db.get(k) for k in GATE_SETTINGS}
        parts.update(policy=policy['config'] if policy else None, review_wanted=db.review_wanted(sid),
                     fallback=db.get('qbit_fallback_' + t['hash']),
                     show=db.one('SELECT state,authorized,selected,auto_download,mapping,cleanup_hold,total FROM shows WHERE id=?', (sid,)),
                     task=[t['title'], t['status'], t.get('acknowledged'), t.get('provider'), t['selection'], t.get('purpose'), t.get('external_id')],
                     candidates=db.rows('''SELECT id,title,source_metadata FROM candidates WHERE show_id=? AND
                         (hash=? OR id IN (SELECT candidate_id FROM source_samples WHERE hash=?)
                         OR id IN (SELECT candidate_id FROM subtitle_samples WHERE hash=?)) ORDER BY id''', (sid, t['hash'], t['hash'], t['hash'])),
                     references=db.rows('SELECT evidence FROM subtitle_samples WHERE hash=? AND show_id=? ORDER BY candidate_id', (t['hash'], sid)))
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    candidate = c()
    db.execute("UPDATE candidates SET hash='a'*40 WHERE id=?", (candidate['id'],))
    for h, sid in (('a' * 40, 42), ('b' * 40, 42), ('c' * 40, 77)):
        db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path) VALUES(?,?,'Example','complete','{}','')", (h, sid))
    tasks = db.rows('SELECT * FROM tasks ORDER BY hash')
    keys = [old_gate_key(t) for t in tasks]
    db.set('qbit_fallback_' + 'b' * 40, {'reason': 'fixture'})
    db.set('source_fidelity', 'original')
    db.execute('INSERT INTO source_policies VALUES(42,?,0)', (json.dumps({'mode': 'selected'}),))
    changed = [old_gate_key(t) for t in tasks]
    assert keys != changed and len(set(changed)) == 3
    with connects() as cost:
        assert [e.gate_key(t) for t in tasks] == changed
    assert cost.count == 3
    qt = {'state': 'uploading', 'progress': 1, 'amount_left': 0}
    t = tasks[0]
    with connects() as cost:
        assert e.recheck_due(t, qt) is True
    assert cost.count == 1
    db.set('task_verified_inputs_' + t['hash'], {'key': e.verification_key(t, qt), 'checked_request': 0})
    db.set('task_verified_' + t['hash'], time.time())
    assert e.recheck_due(t, qt) is False
    db.set('show_check_42', time.time())
    assert e.recheck_due(t, qt) is True
    assert e.recheck_due(t, {**qt, 'state': 'error'}) is True


def test_refresh_scope_loop_skips_present_and_finished_rows_but_still_blocks(delivery, monkeypatch):
    db, scheduler, ready_values, now = delivery
    import anime.subtitle_delivery as module
    checked = []
    original = module._scope
    def scope(db, sid, ep):
        checked.append((sid, ep))
        return original(db, sid, ep)
    monkeypatch.setattr(module, '_scope', scope)
    monkeypatch.setattr(scheduler, 'snapshot', lambda **kwargs: {})
    with db.connect() as c:
        c.execute("UPDATE subtitle_delivery_jobs SET state='accepted' WHERE id='episode:43:2'")
    db.execute("UPDATE shows SET state='paused' WHERE id=43")
    scheduler.refresh()
    # Show 43 left the watching queue: only its non-accepted job needs a scope read.
    assert sorted(set(checked) - {(42, 1), (42, 2)}) == [(43, 1)]
    assert scheduler.get('episode:43:1')['state'] == 'blocked'
    assert scheduler.get('episode:43:2')['state'] == 'accepted'
    assert scheduler.get('episode:42:1')['state'] == 'ready'
