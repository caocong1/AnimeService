"""Plain media progress accepts the same session order as finished releases; late writes never move the position back."""
import json
from fastapi.testclient import TestClient
from test_reliability import setup
from anime.app import create_app


def client_for(setup, tmp_path):
    db, q, e, c = setup; p = tmp_path / 'clip.mp4'; p.write_bytes(b'0123456789')
    (db.path.parent / 'inventory.json').write_text(json.dumps([{'path': str(p), 'size': 10}]), encoding='utf-8')
    client = TestClient(create_app(db, e, False), base_url='http://127.0.0.1:4871')
    headers = {'x-anime-token': client.get('/api/bootstrap').json()['token']}
    return db, client, headers, client.get('/api/web/media').json()[0]['id']


def test_out_of_order_and_foreign_session_writes_are_ignored(setup, tmp_path):
    db, client, headers, key = client_for(setup, tmp_path)
    post = lambda body: client.post(f'/api/web/media/{key}/progress', json=body, headers=headers)
    session = 'session-abcdefghijklmnop'
    assert post({'position': 5, 'duration': 10, 'playing': True, 'session': session, 'sequence': 2}).json() == {'ok': True}
    late = post({'position': 2, 'duration': 10, 'playing': True, 'session': session, 'sequence': 1}).json()
    assert late == {'ok': True, 'ignored': True} and db.one('SELECT position FROM web_progress')['position'] == 5
    assert post({'position': 7, 'duration': 10, 'playing': True, 'session': session, 'sequence': 3}).json() == {'ok': True}
    assert db.one('SELECT position FROM web_progress')['position'] == 7
    assert post({'position': 1, 'duration': 10, 'playing': False, 'session': session, 'sequence': 3}).json()['ignored']
    other = post({'position': 9, 'duration': 10, 'playing': False, 'session': 'another-session-xyz12345', 'sequence': 1}).json()
    assert other == {'ok': True} and db.one('SELECT position FROM web_progress')['position'] == 9
    assert db.one('SELECT sequence FROM finished_playback_receipts WHERE session=?', (session,))['sequence'] == 3
    assert not db.rows('SELECT * FROM watches')


def test_clients_without_a_session_stay_accepted_and_malformed_sessions_are_rejected(setup, tmp_path):
    db, client, headers, key = client_for(setup, tmp_path)
    post = lambda body: client.post(f'/api/web/media/{key}/progress', json=body, headers=headers)
    assert post({'position': 3, 'duration': 10, 'playing': False}).json() == {'ok': True}  # older web clients and the TV
    assert db.one('SELECT position FROM web_progress')['position'] == 3
    assert post({'position': 4, 'duration': 10, 'playing': False, 'session': 'short', 'sequence': 1}).status_code == 400
    assert post({'position': 4, 'duration': 10, 'playing': False, 'session': 'session-abcdefghijklmnop', 'sequence': 0}).status_code == 400
    assert post({'position': 4, 'duration': 10, 'playing': False, 'session': 'session-abcdefghijklmnop'}).status_code == 400
    assert db.one('SELECT position FROM web_progress')['position'] == 3
