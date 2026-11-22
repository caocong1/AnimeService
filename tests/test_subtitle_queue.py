"""The viewer queue projection reads scheduler state only; it writes nothing and leaks nothing private."""
import json
import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from anime.app import create_app
from anime.subtitle_delivery_view import report_progress
from anime.subtitle_queue import production_queue, public_text, STALL_SECONDS
from test_subtitle_delivery import delivery, begin  # noqa: F401  (fixture and dispatch helper)


def item(queue, sid, ep):
    return next(i for i in queue['items'] if i['show_id'] == sid and i['episode'] == ep)


def queued(queue):
    return [(i['show_id'], i['episode'], i['position'], i['ahead']) for i in queue['items'] if i['status'] == 'queued']


def test_ready_episodes_queue_in_snapshot_order_while_the_slot_is_free(delivery):
    db, scheduler, ready, now = delivery
    queue = production_queue(db, now=now[0])
    assert queued(queue) == [(42, 1, 1, 0), (43, 1, 2, 1), (42, 2, 3, 2), (43, 2, 4, 3)]
    assert queue['slot'] == {'occupied': False, 'item': None} and queue['summary']['queued'] == 4
    assert queue['scheduler'] is None  # no dispatch-guard status file in the sandbox
    assert [s['show_id'] for s in queue['shows']] == [42, 43] and queue['shows'][0]['next_unwatched'] == 1
    assert production_queue(db, now=now[0], show=43)['items'] == [i for i in queue['items'] if i['show_id'] == 43]


def test_future_episode_waits_for_release_instead_of_protection_or_dispatch(delivery):
    db, scheduler, _, now = delivery
    db.set('airing_sort_offset_42', 13)
    db.execute("INSERT INTO airings(show_id,episode,airdate,title) VALUES(42,14,'2999-01-24','future local E1')")
    queue = production_queue(db, now=now[0])
    future = item(queue, 42, 1)
    assert future['status'] == 'unreleased' and future['release_date'] == '2999-01-24'
    assert queue['summary']['unreleased'] == 1
    row = scheduler.get('episode:42:1')
    with pytest.raises(ValueError, match='not yet released'):
        scheduler.reserve(row['id'], 'owner', row['revision'])
    db.execute("DELETE FROM subtitle_delivery_jobs WHERE id='episode:42:1'")
    scheduler.refresh()
    assert not db.one("SELECT id FROM subtitle_delivery_jobs WHERE id='episode:42:1'")


def test_dispatched_episode_occupies_the_slot_and_the_rest_wait_behind_it(delivery, monkeypatch):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    starting = production_queue(db, now=now[0])
    assert item(starting, 42, 1)['status'] == 'starting' and starting['slot']['occupied']
    assert queued(starting) == [(43, 1, 1, 1), (42, 2, 2, 2), (43, 2, 3, 3)]
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    queue = production_queue(db, now=now[0])
    head = item(queue, 42, 1)
    assert head['status'] == 'producing' and head['stage'] is None and not head['stalled']
    assert queue['slot']['item'] == {'show_id': 42, 'episode': 1, 'season': 1, 'title': 'Example 42', 'status': 'producing',
                                     'edition': 'v0', 'stage': None, 'since': now[0], 'last_activity': now[0],
                                     'stalled': False, 'stalled_seconds': 0}
    monkeypatch.setattr('anime.subtitle_delivery_view.time', SimpleNamespace(time=lambda: now[0]))
    now[0] += 3600
    report_progress(db, identity, 'actual-fixture-chat', 'layout', 3, 9)
    reported = item(production_queue(db, now=now[0]), 42, 1)
    assert reported['stage'] == 'layout' and reported['progress'] == {'done': 3, 'total': 9}
    assert reported['last_activity'] == now[0] and not reported['stalled']


def test_silence_marks_a_stall_without_touching_the_job(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    now[0] += STALL_SECONDS['starting'] + 1
    unconfirmed = item(production_queue(db, now=now[0]), 42, 1)
    assert unconfirmed['status'] == 'starting' and unconfirmed['stalled'] and unconfirmed['stalled_seconds'] == STALL_SECONDS['starting'] + 1
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    before = scheduler.get(identity)
    now[0] += STALL_SECONDS['producing']
    assert not item(production_queue(db, now=now[0]), 42, 1)['stalled']
    now[0] += 1
    queue = production_queue(db, now=now[0])
    stalled = item(queue, 42, 1)
    assert stalled['stalled'] and stalled['stalled_seconds'] == STALL_SECONDS['producing'] + 1
    assert queue['slot']['item']['stalled'] and queue['summary']['stalled'] == 1
    assert scheduler.get(identity) == before


def test_episodes_without_jobs_show_download_and_inventory_state(delivery):
    db, scheduler, ready, now = delivery
    db.set('download_gaps', {'version': 1, 'checked_at': now[0], 'shows': {'42': {'3': {
        'code': 'download_retry', 'reason': '获取种子失败，等待自动重试', 'retry_at': now[0] + 600, 'checked_at': now[0]}}}})
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status,progress) VALUES(43,3,'',0,'downloading',0.4)")
    db.execute("INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)")
    queue = production_queue(db, now=now[0])
    waiting = item(queue, 42, 3)
    assert waiting['status'] == 'waiting_video' and waiting['gap'] == {
        'code': 'download_retry', 'reason': None, 'retry_at': now[0] + 600, 'checked_at': now[0]}
    downloading = item(queue, 43, 3)
    assert downloading['status'] == 'downloading' and downloading['download_progress'] == 40
    assert not [i for i in queue['items'] if (i['show_id'], i['episode']) == (42, 1)]  # watched episodes leave the queue
    assert queued(queue) == [(43, 1, 1, 0), (42, 2, 2, 1), (43, 2, 3, 2)]
    assert downloading['video_ready_at'] is None and downloading['waiting_seconds'] is None
    db.execute("UPDATE episodes SET status='complete',path='fixture.mkv',size=1,first_completed=? WHERE show_id=43 AND episode=3", (now[0] - 7200,))
    arrived = item(production_queue(db, now=now[0]), 43, 3)
    assert arrived['status'] == 'preparing' and arrived['reasons'] == ['pending_inventory']
    assert arrived['video_ready_at'] == now[0] - 7200 and arrived['waiting_seconds'] == 7200


def test_delivered_v0_shows_its_v1_follow_up(delivery):
    db, scheduler, ready, now = delivery
    db.set('subtitle_delivery_finished_42_1', {'release_id': 'example-s01e01-v0', 'edition': 'v0', 'delivery_status': 'finished',
                                               'manifest_sha256': 'f' * 64, 'accepted': False})
    assert item(production_queue(db, now=now[0]), 42, 1) | {'refinement': None} == item(production_queue(db, now=now[0]), 42, 1)
    plain = item(production_queue(db, now=now[0]), 42, 1)
    assert plain['status'] == 'v0_ready' and plain['release_id'] == 'example-s01e01-v0' and plain['refinement'] is None
    db.execute('''INSERT INTO subtitle_delivery_jobs(id,show_id,episode,kind,state,input_key,payload,dispatch_state,thread_id,created,updated)
                  VALUES(?,42,1,'refinement','production','refinement-input',?,'acknowledged','actual-fixture-chat',?,?)''',
               ('refinement:42:1:fixture', json.dumps({'assessment': {}, 'edition': 'v1'}), now[0], now[0]))
    queue = production_queue(db, now=now[0] + 60)
    refining = item(queue, 42, 1)
    assert refining['status'] == 'v0_ready' and refining['refinement']['status'] == 'producing' and refining['refinement']['edition'] == 'v1'
    assert queue['slot']['occupied'] and queue['slot']['item']['edition'] == 'v1' and queue['slot']['item']['episode'] == 1
    assert queue['summary']['producing'] == 0 and queue['work_summary']['producing'] == 1
    assert queued(queue)[0] == (43, 1, 1, 1)
    db.set('subtitle_delivery_finished_42_1', {'release_id': 'example-s01e01-v1', 'edition': 'v1', 'delivery_status': 'finished',
                                               'manifest_sha256': 'e' * 64, 'accepted': False})
    assert item(production_queue(db, now=now[0]), 42, 1)['status'] == 'done'


def test_api_route_filters_by_show_and_exposes_nothing_private(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    scheduler.record_blocked(identity, 'actual-fixture-chat', ['encoder missing at C:\\tools\\ffmpeg.exe'], owner_stopped=True)
    engine = Mock()
    engine.db = engine.subtitle_studio.db = db
    client = TestClient(create_app(db, engine, start_worker=False), base_url='http://127.0.0.1:4871')
    page = client.get('/subtitle-queue')
    assert page.status_code == 200 and page.headers['content-type'].startswith('text/html')
    everything = client.get('/api/subtitle-queue').json()
    only = client.get('/api/subtitle-queue?show=43').json()
    assert {i['show_id'] for i in everything['items']} == {42, 43} and {i['show_id'] for i in only['items']} == {43}
    blocked = item(everything, 42, 1)
    assert blocked['status'] == 'blocked' and blocked['reasons'] == ['production_blocked']
    progress = client.get('/api/subtitle-progress')
    assert progress.status_code == 200 and progress.json()['live'] is True
    public_job = next(j for j in progress.json()['jobs'] if j['job_id'] == identity)
    assert public_job['work']['status'] == blocked['status']
    assert public_job['work']['stop_class'] == blocked['stop_class'] == 'protected'
    assert not everything['slot']['occupied']  # a stopped owner frees the slot
    text = json.dumps(everything, ensure_ascii=False)
    text += progress.text
    for private in ('actual-fixture-chat', 'test-owner', 'thread_id', 'lease_owner', 'lease_token', 'dispatch_token',
                    str(db.path.parent), 'ffmpeg.exe'):
        assert private not in text
    assert client.get('/api/subtitle-queue?show=abc').status_code == 422


def guard_status(db, job, decision, at, last_log_at, revision=None):
    """The owner guard's status file as the production task writes it, with the chat identifiers it keeps."""
    path = db.path.parent.parent / 'data/diagnostics/codex-subtitle-owner-guard/status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'at': at, 'apply': True, 'error': None, 'threads': [{
        'id': job['id'], 'thread_id': job['thread_id'], 'input_key': job['input_key'],
        'revision': job['revision'] if revision is None else revision, 'turn_id': 'turn-9',
        'last_log_at': last_log_at, 'decision': decision}]}), encoding='utf-8')


def test_owner_guard_signal_overrides_the_silence_heuristic(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    now[0] += 2 * 3600  # nothing recorded in the ledger since the dispatch
    job = scheduler.get(identity)
    guard_status(db, job, 'recent_progress', now[0] - 60, now[0] - 90)
    working = item(production_queue(db, now=now[0]), 42, 1)
    assert working['owner_state'] == 'working' and working['owner_last_activity'] == now[0] - 90 and not working['stalled']
    guard_status(db, job, 'terminal_or_idle', now[0] - 60, now[0] - 20 * 60)
    idle = item(production_queue(db, now=now[0]), 42, 1)
    assert idle['owner_state'] == 'idle' and idle['stalled'] and idle['stalled_seconds'] == 20 * 60
    guard_status(db, job, 'terminal_or_idle', now[0] - 60, now[0] - 5 * 60)
    assert not item(production_queue(db, now=now[0]), 42, 1)['stalled']  # just finished a turn; the dispatcher's next round may continue it
    guard_status(db, job, 'interrupt_stale_owner_wait', now[0] - 60, now[0] - 30)
    stuck = item(production_queue(db, now=now[0]), 42, 1)
    assert stuck['owner_state'] == 'stuck' and stuck['stalled']
    guard_status(db, job, 'recent_progress', now[0] - 3600, now[0] - 3600)  # a stale guard record is ignored
    stale = item(production_queue(db, now=now[0]), 42, 1)
    assert stale['owner_state'] is None and stale['stalled'] and stale['stalled_seconds'] == 2 * 3600
    guard_status(db, job, 'recent_progress', now[0] - 60, now[0] - 60, revision=job['revision'] - 1)  # an earlier dispatch's look is ignored too
    assert item(production_queue(db, now=now[0]), 42, 1)['owner_state'] is None
    text = json.dumps(production_queue(db, now=now[0]))
    for private in ('actual-fixture-chat', 'turn-9', 'turn_id', 'thread_id'):
        assert private not in text


@pytest.mark.parametrize('invalid', ['not_object', 'error', 'future', 'nan', 'bad_time', 'bad_threads',
                                    'bad_row', 'bad_last_log', 'future_last_log', 'wrong_owner',
                                    'duplicate', 'oversized', 'invalid_utf8'])
def test_invalid_owner_guard_never_hides_silence_or_breaks_the_queue(delivery, invalid):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    now[0] += 2 * 3600
    job = scheduler.get(identity)
    guard_status(db, job, 'recent_progress', now[0] - 60, now[0] - 90)
    path = db.path.parent.parent / 'data/diagnostics/codex-subtitle-owner-guard/status.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    if invalid == 'not_object': value = []
    elif invalid == 'error': value['error'] = 'synthetic partial read failure'
    elif invalid == 'future': value['at'] = now[0] + 3600
    elif invalid == 'nan': value['at'] = float('nan')
    elif invalid == 'bad_time': value['at'] = 'bad'
    elif invalid == 'bad_threads': value['threads'] = 'bad'
    elif invalid == 'bad_row': value['threads'] = [None]
    elif invalid == 'bad_last_log': value['threads'][0]['last_log_at'] = 'bad'
    elif invalid == 'future_last_log': value['threads'][0]['last_log_at'] = now[0] + 3600
    elif invalid == 'wrong_owner': value['threads'][0]['thread_id'] = 'another-synthetic-owner'
    elif invalid == 'duplicate': value['threads'].append(dict(value['threads'][0]))
    path.write_text(json.dumps(value), encoding='utf-8')
    if invalid == 'oversized': path.write_bytes(b' ' * (256 * 1024 + 1))
    if invalid == 'invalid_utf8': path.write_bytes(b'\xff')
    row = item(production_queue(db, now=now[0]), 42, 1)
    assert row['owner_state'] is None and row['stalled']


def test_changed_before_control_is_not_a_stuck_owner_verdict(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    now[0] += 60
    guard_status(db, scheduler.get(identity), 'changed_before_control', now[0], now[0])
    row = item(production_queue(db, now=now[0]), 42, 1)
    assert row['owner_state'] == 'unknown' and not row['stalled']


def test_parallel_capacity_changes_queue_ahead_and_capacity_projection(delivery):
    db, scheduler, _, now = delivery
    db.set('subtitle_delivery_policy', {'max_active_productions': 3})
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    queue = production_queue(db, now=now[0])
    assert queue['capacity'] == {'limit': 3, 'occupied': 1, 'available': 2}
    assert queued(queue) == [(43, 1, 1, 0), (42, 2, 2, 0), (43, 2, 3, 1)]
    assert all(i['capacity_available'] == 2 for i in queue['items'] if i['status'] == 'queued')


def test_ready_v1_refinement_waits_in_the_same_line(delivery):
    db, scheduler, ready, now = delivery
    db.set('subtitle_delivery_finished_42_1', {'release_id': 'example-s01e01-v0', 'edition': 'v0', 'delivery_status': 'finished',
                                               'manifest_sha256': 'f' * 64, 'accepted': False})
    db.execute('''INSERT INTO subtitle_delivery_jobs(id,show_id,episode,kind,state,input_key,payload,created,updated)
                  VALUES(?,42,1,'refinement','ready','refinement-input',?,?,?)''',
               ('refinement:42:1:fixture', json.dumps({'assessment': {}, 'edition': 'v1'}), now[0], now[0]))
    queue = production_queue(db, now=now[0])
    v0 = item(queue, 42, 1)
    assert v0['status'] == 'v0_ready' and v0['refinement']['status'] == 'queued'
    assert (v0['refinement']['position'], v0['refinement']['ahead']) == (1, 0)  # refinements are claimed first today
    assert queued(queue) == [(43, 1, 2, 1), (42, 2, 3, 2), (43, 2, 4, 3)]
    assert 'order' not in v0 and all('order' not in i for i in queue['items'])


def test_public_text_hides_local_paths_but_keeps_times():
    assert public_text('encoder missing at C:\\synthetic\\tools\\ffmpeg.exe and /opt/synthetic/core.py') == 'encoder missing at [路径] and [路径]'
    assert public_text('retry 2026-10-19 12:00 after 3/4 attempts') == 'retry 2026-10-19 12:00 after 3/4 attempts'
    assert len(public_text('x' * 500)) == 200


def test_owner_prose_and_unknown_assessment_strings_are_not_public(delivery):
    db,scheduler,_,now=delivery
    identity,intent=begin(scheduler)
    scheduler.reconcile_dispatch(identity,intent['dispatch_token'],thread_id='sensitive-responsible-id')
    scheduler.record_blocked(identity,'sensitive-responsible-id',
        ['account sensitive-account; dispatch_token=sensitive-authority; host sensitive-host'],owner_stopped=True)
    text=json.dumps(production_queue(db,now=now[0]))
    for secret in ['sensitive-account','sensitive-authority','sensitive-host','sensitive-responsible-id']:
        assert secret not in text


def test_projection_uses_one_database_snapshot(delivery,monkeypatch):
    db,_,_,now=delivery
    import anime.read_projection as projection
    original=projection.sqlite3.connect;calls=[]
    def connect(*args,**kwargs):calls.append(args[0]);return original(*args,**kwargs)
    monkeypatch.setattr(projection.sqlite3,'connect',connect)
    result=production_queue(db,now=now[0])
    assert len(calls)==1 and str(calls[0]).endswith('?mode=ro')
    assert result['summary']['queued']==4


def test_nested_recovery_monitor_health_is_projected_without_private_details(delivery):
    db, _, _, now = delivery
    path = db.path.parent.parent / 'data/diagnostics/codex-dispatch-guard/status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'at': now[0], 'error': None, 'threads': [],
        'preparation_recovery': {'checked_at': now[0] - 4000, 'error': 'private path must not be exported',
                                 'execution': {'status': 'stalled'}}}), encoding='utf-8')
    health = production_queue(db, now=now[0])['scheduler']
    assert health['recovery_ok'] is False and health['recovery_stale'] is True
    assert health['recovery_execution_stalled'] is True
    assert 'private path' not in json.dumps(health)


@pytest.mark.parametrize('raw', ['[]', '{broken', '{"at":1000,"threads":"invalid"}',
    '{"at":1000,"threads":[],"preparation_recovery":"invalid"}',
    '{"at":1000,"threads":[],"preparation_recovery":{"checked_at":1000,"execution":"invalid"}}'])
def test_malformed_guard_health_is_visible_and_does_not_crash_pages(delivery, raw):
    db, _, _, now = delivery
    path = db.path.parent.parent / 'data/diagnostics/codex-dispatch-guard/status.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding='utf-8')
    health = production_queue(db, now=now[0])['scheduler']
    assert health is not None and (health['ok'] is False or health.get('recovery_ok') is False)


def test_unowned_scope_protection_is_not_a_production_pause(delivery):
    db, _, _, now = delivery
    db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    queue = production_queue(db, now=now[0])
    protected = item(queue, 42, 1)
    assert protected['status'] == 'protected' and protected['reasons'] == ['scope_protected']
    assert queue['summary']['protected'] == 2 and not queue['slot']['occupied']


def test_scope_change_also_protects_a_stopped_owner(delivery):
    db, scheduler, _, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    scheduler.record_blocked(identity, 'actual-fixture-chat', ['stopped before scope change'], owner_stopped=True)
    db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    protected = item(production_queue(db, now=now[0]), 42, 1)
    assert protected['status'] == 'protected' and protected['reasons'] == ['scope_protected']


def test_v0_and_refinement_recovery_are_bound_to_their_own_jobs(delivery):
    db, _, _, now = delivery
    db.set('subtitle_delivery_finished_42_1', {'release_id': 'example-s01e01-v0', 'edition': 'v0', 'delivery_status': 'finished',
                                               'manifest_sha256': 'f' * 64, 'accepted': False})
    payload = {'assessment': {'ready': True, 'reasons': []}, 'edition': 'v1', 'baseline_release_id': 'example-s01e01-v0',
               'owner_stopped_writing': True, 'recovery_class': 'system', 'recovery_turn': 'v1-turn'}
    db.execute('''INSERT INTO subtitle_delivery_jobs(id,show_id,episode,kind,state,input_key,payload,dispatch_state,thread_id,created,updated)
                  VALUES(?,42,1,'refinement','blocked','refinement-input',?,'blocked-stopped','actual-fixture-chat',?,?)''',
               ('refinement:42:1:fixture', json.dumps(payload), now[0], now[0]))
    v0 = item(production_queue(db, now=now[0]), 42, 1)
    assert v0['status'] == 'v0_ready' and 'recovery' not in v0
    assert v0['refinement']['status'] == 'blocked'
    assert v0['refinement']['recovery']['domain'] == 'owner_recovery'
    assert '_job_id' not in v0['refinement'] and '_job_id' not in v0
    db.execute("UPDATE subtitle_delivery_jobs SET dispatch_state='unknown',updated=0 WHERE id='refinement:42:1:fixture'")
    late = production_queue(db, now=now[0] + 22000)
    assert late['summary']['reconciling'] == 1
    db.execute("DELETE FROM subtitle_delivery_jobs WHERE id='refinement:42:1:fixture'")
    original = db.one("SELECT payload FROM subtitle_delivery_jobs WHERE id='episode:42:1'")
    payload = json.loads(original['payload'])
    payload.update(refinement_pending=True, owner_stopped_writing=True,
                   recovery_class='system', recovery_turn='same-job-turn')
    db.execute("UPDATE subtitle_delivery_jobs SET state='blocked',dispatch_state='unknown',payload=?,updated=0 WHERE id='episode:42:1'",
               (json.dumps(payload),))
    same = item(production_queue(db, now=now[0] + 22000), 42, 1)
    assert same['recovery']['status'] == 'reconciling'
    assert same['refinement']['recovery']['status'] == 'reconciling'
    assert production_queue(db, now=now[0] + 22000)['summary']['reconciling'] == 1


def test_preparation_header_excludes_real_overdue_and_blocked_recovery(delivery, tmp_path, monkeypatch):
    from anime.subtitle_recovery import record_attempt
    from test_subtitle_preparation import media
    db, scheduler, ready, now = delivery
    media(db, tmp_path)
    now[0] = 2000
    monkeypatch.setattr('anime.subtitle_recovery.time.time', lambda: now[0])
    ready.values[(42, 1)] = {**ready(db, 42, 1, {}), 'capability_ready': False,
                            'reasons': ['needs_verified_local_finished_output_tools']}
    scheduler.refresh()
    # This fixture supplies material already, so refresh conservatively blocks
    # the incomplete capability assessment. Exercise a preparation-stage job,
    # as persisted by the real material-preparation path.
    db.execute("UPDATE subtitle_delivery_jobs SET state='preparing' WHERE id='episode:42:1'")
    initial = production_queue(db, now=now[0])
    assert item(initial, 42, 1)['recovery']['status'] == 'actionable'
    assert initial['work_summary']['active_preparation'] == 1
    now[0] += 22000
    overdue = production_queue(db, now=now[0])
    assert item(overdue, 42, 1)['recovery']['status'] == 'overdue'
    assert overdue['work_summary']['preparing'] == 1  # Original job state remains available.
    assert overdue['work_summary']['active_preparation'] == 0
    evidence = db.path.parent / 'subtitle-studio' / 'fixture-output-blocked.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"output":"fixture dependency missing"}', encoding='utf-8')
    record_attempt(db, scheduler.snapshot()['recovery'], 'episode:42:1', 'verify_output',
                   'blocked', [evidence], now=now[0])
    before = {t: db.rows(f'SELECT * FROM {t}') for t in
              ('subtitle_delivery_jobs', 'episodes', 'watches', 'tasks')}
    blocked = production_queue(db, now=now[0])
    assert item(blocked, 42, 1)['recovery']['status'] == 'repair_blocked'
    assert blocked['work_summary']['active_preparation'] == 0
    assert all(db.rows(f'SELECT * FROM {t}') == value for t, value in before.items())
