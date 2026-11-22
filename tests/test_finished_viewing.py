import contextlib
import json
import math
import sqlite3
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.db import Store


@pytest.fixture
def finished(tmp_path):
    db = Store(tmp_path / 'test.db')
    db.execute("INSERT INTO shows(id,title,total,selected,state,mapping) VALUES(42,'测试',2,1,'watching',?)", (json.dumps({'season': 2}),))
    db.execute("INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)")
    db.execute('UPDATE shows SET watched=1 WHERE id=42')
    engine = Mock()
    engine.subtitle_studio.production_targets.lease.return_value = {'ok': True}
    app = create_app(db, engine, start_worker=False)
    app.state.finished_viewing.root = tmp_path
    catalog = tmp_path / 'static/finished-media'
    releases = []
    for v in (0, 1):
        identity = f'test-v{v}'
        folder = catalog / identity
        folder.mkdir(parents=True)
        (folder / 'episode.mp4').write_bytes(b'test media')
        releases.append(dict(id=identity,show_id=42,season=2,episode=2,status='finished',video_sha256=str(v)*64))
    (catalog / 'index.json').write_text(json.dumps({'releases': releases}), encoding='utf-8')
    client = TestClient(app, base_url='http://127.0.0.1:4871')
    client.headers['X-Anime-Token'] = client.get('/api/bootstrap').json()['token']
    return client, db, engine, catalog


def packet(sequence=1, **kw):
    return dict(position=123,duration=1420,playing=True,session='test-session-00000001',sequence=sequence,**kw)


def test_resume_persists_only_for_exact_release_and_does_not_mark_watched(finished):
    client, db, _, _ = finished
    assert client.post('/api/finished-viewing/test-v0/progress',json=packet()).status_code == 200
    v0 = client.get('/api/finished-viewing/test-v0').json()
    assert v0['progress']['position'] == 123 and v0['progress']['last_played']
    assert client.get('/api/finished-viewing/test-v1').json()['progress'] is None
    assert v0['finished'] is False
    assert db.one('SELECT watched FROM shows')['watched'] == 1
    assert not db.rows('SELECT * FROM episodes')
    assert client.post('/api/finished-viewing/test-v0/progress',json={**packet(2),'position':1420}).status_code == 200
    assert not client.get('/api/finished-viewing/test-v0').json()['finished']


def test_manual_watch_and_unwatch_share_normal_episode_record(finished):
    client, db, engine, _ = finished
    response = client.post('/api/finished-viewing/test-v0/watch',json={'finished':True})
    assert response.json() == {'ok':True,'state':'completed'}
    engine.state.assert_called_once_with(42,'completed')
    assert db.one('SELECT watched FROM shows')['watched'] == 2
    assert client.get('/api/finished-viewing/test-v1').json()['finished']
    client.post('/api/shows/42/watch',json={'episode':2,'finished':False})
    client.post('/api/finished-viewing/test-v0/progress',json={**packet(),'position':1420})
    assert not client.get('/api/finished-viewing/test-v1').json()['finished']
    assert db.one('SELECT watched FROM shows')['watched'] == 1


def test_delayed_progress_cannot_overwrite_final_position(finished):
    client, _, _, _ = finished
    path = '/api/finished-viewing/test-v0/progress'
    client.post(path,json={**packet(3),'position':400})
    assert client.post(path,json=packet(2)).json()['ignored']
    assert client.post(path,json=packet(3)).json()['ignored']
    assert client.get('/api/finished-viewing/test-v0').json()['progress']['position'] == 400


@pytest.mark.parametrize('bad', [{'position':-1},{'duration':0},{'position':1500},{'position':'bad'},{'sequence':False},{'session':'x'}])
def test_invalid_progress_is_rejected_without_mutation(finished,bad):
    client, db, _, _ = finished
    assert client.post('/api/finished-viewing/test-v0/progress',json={**packet(),**bad}).status_code == 400
    assert not db.rows('SELECT * FROM web_progress')


def test_exact_content_and_current_season_protect_progress_and_watch(finished):
    client, db, _, catalog = finished
    client.post('/api/finished-viewing/test-v0/progress',json=packet())
    index = json.loads((catalog/'index.json').read_text())
    index['releases'][0]['video_sha256'] = 'a'*64
    (catalog/'index.json').write_text(json.dumps(index))
    assert client.get('/api/finished-viewing/test-v0').json()['progress'] is None
    db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps({'season':3}),))
    assert client.post('/api/finished-viewing/test-v0/watch',json={'finished':True}).status_code == 404
    assert client.get('/api/finished-viewing').json() == {'releases':[]}
    assert db.one('SELECT watched FROM shows')['watched'] == 1


def test_missing_file_and_csrf_cannot_write(finished):
    client, db, _, catalog = finished
    (catalog/'test-v0/episode.mp4').unlink()
    assert client.post('/api/finished-viewing/test-v0/progress',json=packet()).status_code == 404
    assert client.post('/api/finished-viewing/test-v1/watch',json={'finished':True},headers={'X-Anime-Token':'bad'}).status_code == 403
    assert not db.rows('SELECT * FROM web_progress')


def test_finished_player_lease_protects_same_show_episode(finished):
    client, _, engine, _ = finished
    client.post('/api/finished-viewing/test-v0/viewing-session',json={'session':'test-session-00000001'})
    args = engine.subtitle_studio.production_targets.lease.call_args.args
    assert args[0]['show_id'] == 42 and args[0]['episode'] == 2
    assert args[0]['id'].startswith('finished:')


def test_delivery_uses_normal_player_media_without_replacing_original_episode(finished):
    client, db, engine, _ = finished
    before = db.rows('SELECT * FROM episodes')
    key = 'finished:test-v0'
    media = client.get('/api/web/media/' + key).json()
    assert media['release_id'] == 'test-v0' and media['show_id'] == 42 and media['episode'] == 2
    assert 'path' not in media and media['progress'] is None
    assert client.get('/api/web/media/' + key + '/subtitles').json() == {
        'tracks': [], 'default': None, 'burned_in': True}
    stream = client.get('/api/web/media/' + key + '/stream', headers={'Range': 'bytes=0-3'})
    assert stream.status_code == 206 and stream.content == b'test'
    assert client.post('/api/web/media/' + key + '/progress', json=packet()).status_code == 200
    assert client.get('/api/web/media/' + key).json()['progress']['position'] == 123
    assert client.get('/api/web/media/finished:test-v1').json()['progress'] is None
    assert not client.post('/api/web/media/' + key + '/progress', json={'position':500,'duration':1420}).is_success
    client.post('/api/web/media/' + key + '/viewing-session', json={'session':'test-session-00000002'})
    assert engine.subtitle_studio.production_targets.lease.call_args.args[0]['episode'] == 2
    assert db.rows('SELECT * FROM episodes') == before
    assert client.get('/api/web/media/finished:missing').status_code == 404


@pytest.mark.parametrize('count', [133, 405])
def test_catalog_reads_large_library_in_one_connection(finished, monkeypatch, count):
    client, db, _, catalog = finished
    releases = []
    for i in range(count):
        identity = f'bulk-{i}'
        folder = catalog / identity
        folder.mkdir()
        (folder / 'episode.mp4').write_bytes(b'media')
        releases.append(dict(id=identity, show_id=42, season=2, episode=2,
                             status='finished', video_sha256='a'*64))
    (catalog / 'index.json').write_text(json.dumps({'releases': releases}), encoding='utf-8')
    opened, queries = [], []
    original = db.connect

    @contextlib.contextmanager
    def counted():
        opened.append(1)
        with original() as connection:
            connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
            connection.execute('PRAGMA query_only=ON')
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(db, 'connect', counted)
    response = client.get('/api/finished-viewing')
    assert response.status_code == 200
    rows = response.json()['releases']
    assert [r['id'] for r in rows] == [r['id'] for r in releases]
    assert all(r['progress'] is None and r['finished'] is False for r in rows)
    assert len(opened) == 1
    assert sum(q.lstrip().startswith('WITH requested') for q in queries) == math.ceil(count / 200)


def test_item_does_not_inspect_unrelated_media(finished, monkeypatch):
    client, _, _, catalog = finished
    path_type = type(catalog)
    original = path_type.stat
    checked = []

    def stat(path, *args, **kwargs):
        if path.name == 'episode.mp4': checked.append(path.parent.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(path_type, 'stat', stat)
    assert client.get('/api/finished-viewing/test-v1').status_code == 200
    assert checked == ['test-v1']


def test_catalog_rechecks_live_progress_watches_state_and_files(finished):
    client, db, _, catalog = finished
    assert len(client.get('/api/finished-viewing').json()['releases']) == 2
    client.post('/api/finished-viewing/test-v0/progress', json=packet())
    client.post('/api/finished-viewing/test-v0/watch', json={'finished': True})
    # The fixture's engine.state is a mock; apply its normal state change.
    db.execute("UPDATE shows SET state='completed' WHERE id=42")
    rows = client.get('/api/finished-viewing').json()['releases']
    assert rows[0]['progress']['position'] == 123 and rows[1]['progress'] is None
    assert all(r['finished'] is True and r['show_state'] == 'completed' for r in rows)
    (catalog / 'test-v0/episode.mp4').unlink()
    (catalog / 'test-v1/episode.mp4').write_bytes(b'')
    assert client.get('/api/finished-viewing').json()['releases'] == []


def test_catalog_excludes_invalid_or_unmapped_releases(finished):
    client, _, _, catalog = finished
    path = catalog / 'index.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    template = data['releases'][0]
    data['releases'] = [
        {**template, **invalid} for invalid in (
            {'id': '../secret'}, {'episode': True}, {'episode': 0}, {'season': 3},
            {'show_id': 999}, {'video_sha256': 'invalid'}, {'status': 'rendering'},
        )
    ] + data['releases']
    path.write_text(json.dumps(data), encoding='utf-8')
    rows = client.get('/api/finished-viewing').json()['releases']
    assert [r['id'] for r in rows] == ['test-v0', 'test-v1']
