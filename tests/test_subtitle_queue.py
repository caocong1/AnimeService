"""The viewer queue projection reads scheduler state only; it writes nothing and leaks nothing private."""
import json
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
        'code': 'download_retry', 'reason': '获取种子失败，等待自动重试', 'retry_at': now[0] + 600, 'checked_at': now[0]}
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
    assert queued(queue)[0] == (43, 1, 1, 1)
    db.set('subtitle_delivery_finished_42_1', {'release_id': 'example-s01e01-v1', 'edition': 'v1', 'delivery_status': 'finished',
                                               'manifest_sha256': 'e' * 64, 'accepted': False})
    assert item(production_queue(db, now=now[0]), 42, 1)['status'] == 'done'


def test_api_route_filters_by_show_and_exposes_nothing_private(delivery):
    db, scheduler, ready, now = delivery
    identity, intent = begin(scheduler)
    scheduler.reconcile_dispatch(identity, intent['dispatch_token'], thread_id='actual-fixture-chat')
    scheduler.record_blocked(identity, 'actual-fixture-chat', ['encoder missing at C:\\tools\\ffmpeg.exe'], owner_stopped=True)
    client = TestClient(create_app(db, Mock(), start_worker=False), base_url='http://127.0.0.1:4871')
    page = client.get('/subtitle-queue')
    assert page.status_code == 200 and page.headers['content-type'].startswith('text/html')
    everything = client.get('/api/subtitle-queue').json()
    only = client.get('/api/subtitle-queue?show=43').json()
    assert {i['show_id'] for i in everything['items']} == {42, 43} and {i['show_id'] for i in only['items']} == {43}
    blocked = item(everything, 42, 1)
    assert blocked['status'] == 'blocked' and blocked['reasons'] == ['encoder missing at [路径]']
    assert not everything['slot']['occupied']  # a stopped owner frees the slot
    text = json.dumps(everything, ensure_ascii=False)
    for private in ('actual-fixture-chat', 'test-owner', 'thread_id', 'lease_owner', 'lease_token', 'dispatch_token',
                    str(db.path.parent), 'ffmpeg.exe'):
        assert private not in text
    assert client.get('/api/subtitle-queue?show=abc').status_code == 422


def test_public_text_hides_local_paths_but_keeps_times():
    assert public_text('encoder missing at C:\\synthetic\\tools\\ffmpeg.exe and /opt/synthetic/core.py') == 'encoder missing at [路径] and [路径]'
    assert public_text('retry 2026-10-19 12:00 after 3/4 attempts') == 'retry 2026-10-19 12:00 after 3/4 attempts'
    assert len(public_text('x' * 500)) == 200
