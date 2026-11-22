import contextlib
import hashlib
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from anime.app import create_app
from anime.db import DEFAULTS, Store
from anime.engine import Engine
from anime.source_blacklist import KEY


def setup(tmp_path):
    db = Store(tmp_path / 'test.db')
    engine = Engine(db, qbit=Mock(), sources=Mock(), library=tmp_path / 'library')
    client = TestClient(create_app(db, engine, start_worker=False), base_url='http://127.0.0.1:4871')
    return db, engine, client


def count_reads(db, monkeypatch):
    opened, reads = [], []
    original = db.connect

    @contextlib.contextmanager
    def counted():
        opened.append(1)
        with original() as c:
            c.set_trace_callback(lambda sql: reads.append(sql) if sql.lstrip().startswith('SELECT') else None)
            yield c

    monkeypatch.setattr(db, 'connect', counted)
    return opened, reads


@pytest.mark.parametrize('count', [1, 90, 405])
def test_home_reads_progress_in_bounded_batches_and_keeps_recent_resume_order(tmp_path, monkeypatch, count):
    db, engine, client = setup(tmp_path)
    for sid in range(1, count + 1):
        db.upsert_subject({'id': sid, 'name': f'Example {sid}', 'total_episodes': 1})
        db.execute("UPDATE shows SET selected=1,state='watching' WHERE id=?", (sid,))
        path = tmp_path / f'{sid}.mp4'
        path.write_bytes(b'video')
        db.execute("INSERT INTO episodes(show_id,episode,path,size,status,first_completed) VALUES(?,1,?,5,'complete',?)",
                   (sid, str(path), sid))
        media_id = hashlib.sha256(str(path).lower().encode()).hexdigest()[:32]
        db.execute('INSERT INTO web_progress(media_id,position,duration,last_played) VALUES(?,100,1400,?)',
                   (media_id, count + 1 - sid))
    opened, reads = count_reads(db, monkeypatch)
    data = client.get('/api/home').json()
    assert len(opened) == 1 and len(reads) == 3 + (count + 399) // 400
    assert data['unwatched'] == count and data['watching'] == count
    assert [r['show_id'] for r in data['resume']] == list(range(1, min(count, 8) + 1))
    assert [r['show_id'] for r in data['next']] == list(range(count, max(0, count - 18), -1))
    assert all(not {'path', 'size', 'source_title', 'source_selection'} & row.keys() for row in data['next'])
    assert not engine.qb.mock_calls and not engine.sources.mock_calls


def test_home_sees_watch_progress_state_file_and_blacklist_changes_on_next_request(tmp_path):
    db, engine, client = setup(tmp_path)
    db.upsert_subject({'id': 42, 'name': 'Example', 'total_episodes': 2})
    db.execute("UPDATE shows SET selected=1,state='watching' WHERE id=42")
    paths = [tmp_path / f'{n}.mp4' for n in (1, 2)]
    selection = {'ids': [0], 'files': [{'index': 0, 'name': '[Excluded] Example 2.mp4'}]}
    db.execute("INSERT INTO tasks(hash,show_id,title,selection) VALUES('copy',42,'[Other] Example',?)",
               (json.dumps(selection),))
    for n, path in enumerate(paths, 1):
        path.write_bytes(b'video')
        db.execute("INSERT INTO episodes(show_id,episode,path,size,status,hash) VALUES(42,?,?,5,'complete',?)",
                   (n, str(path), 'copy' if n == 2 else None))
    data = client.get('/api/home').json()
    key = data['next'][0]['media_id']
    db.execute('INSERT INTO web_progress(media_id,position,duration,last_played) VALUES(?,100,1400,10)', (key,))
    assert client.get('/api/home').json()['resume'][0]['progress']['position'] == 100
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    assert client.get('/api/home').json()['next'][0]['episode'] == 2
    db.set(KEY, [{'publisher': 'Excluded', 'aliases': ['Excluded'], 'reason': 'fixture'}])
    assert client.get('/api/home').json()['unwatched'] == 0
    db.set(KEY, [])
    paths[1].write_bytes(b'changed')
    assert client.get('/api/home').json()['unwatched'] == 0
    paths[1].unlink()
    paths[1].mkdir()
    assert not client.get('/api/home').json()['next']
    paths[1].rmdir()
    paths[1].write_bytes(b'video')
    for state in ('paused', 'dropped', 'completed'):
        db.execute('UPDATE shows SET state=? WHERE id=42', (state,))
        assert not client.get('/api/home').json()['next']
    assert paths[0].read_bytes() == b'video'
    assert db.one('SELECT finished FROM watches WHERE show_id=42 AND episode=1')['finished'] == 1
    assert not engine.qb.mock_calls and not engine.sources.mock_calls


def test_bootstrap_reads_settings_together_and_sees_updates(tmp_path, monkeypatch):
    db, _, client = setup(tmp_path)
    db.set('resolution', None)
    db.upsert_subject({'id': 42, 'name': 'Example', 'date': '2024-01-01'})
    opened, reads = count_reads(db, monkeypatch)
    data = client.get('/api/bootstrap').json()
    assert len(opened) == 1 and len(reads) == 2
    assert data['settings'] == {**DEFAULTS, 'resolution': None} and data['local']
    db.set('resolution', '720')
    assert client.get('/api/bootstrap').json()['settings']['resolution'] == '720'


def test_viewing_projection_keeps_every_home_field_and_full_response_is_available(tmp_path):
    db, engine, client = setup(tmp_path)
    db.upsert_subject({'id': 42, 'name': 'Example', 'total_episodes': 2, 'date': '2999-01-01'})
    db.execute("UPDATE shows SET selected=1,state='watching',summary='Full detail' WHERE id=42")
    db.execute("INSERT INTO quarter_members VALUES(42,'2026-10','continuing','fixture')")
    for scope in ('home', 'history'):
        full = client.get('/api/shows?scope=' + scope).json()
        light = client.get('/api/shows?scope=' + scope + '&viewing=true').json()
        assert len(full) == len(light) == 1
        assert all(value == full[0][key] for key, value in light[0].items())
        assert light[0]['air_date'] == '2999-01-01'
        assert light[0]['quarters'] == ['2026-10'] and light[0]['airing_schedule_version'] == 1
        assert {'title', 'original', 'image', 'selected', 'state', 'watched', 'total', 'unwatched',
                'air_date', 'quarters', 'quarter', 'mapping', 'eps', 'next', 'download_updated'} <= light[0].keys()
        assert full[0]['summary'] == 'Full detail' and 'summary' not in light[0]
    assert not engine.qb.mock_calls and not engine.sources.mock_calls
