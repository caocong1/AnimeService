import copy
import json
import time
from unittest.mock import Mock
import pytest
import requests
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.bangumi import BangumiClient, BangumiError, episode_map
from anime.bangumi_auth import TokenVault
from anime.bangumi_sync import BangumiSync
from anime.db import Store


class MemoryVault:
    def __init__(self): self.value = None
    def read(self): return self.value
    def save(self, value): self.value = value
    def clear(self): self.value = None


class FakeBangumi:
    def __init__(self):
        self.user = {'id': 77, 'username': 'test-user', 'nickname': '测试'}
        self.collections = {}
        self.progress = {}
        self.writes = []
        self.calls = []
        self.numbering = [{'id': 101, 'type': 0, 'sort': 49, 'ep': 1},
                          {'id': 102, 'type': 0, 'sort': 50, 'ep': 2}]
        self.fail = None
        self.timeout_after_episode_write = False
        self.timeout_after_collection_write = False
        self.on_read = None

    def me(self): return self.user.copy()
    def request(self, method, path):
        self.calls.append((method, path))
        if self.fail: raise self.fail
        return {'id': int(path.rsplit('/', 1)[1]), 'type': 2}
    def collection(self, username, sid):
        if self.on_read:
            callback, self.on_read = self.on_read, None
            callback()
        return copy.deepcopy(self.collections.get(sid))
    def episodes(self, sid): return copy.deepcopy(self.numbering)
    def episode_collections(self, sid):
        return [{'episode': {'id': eid}, 'type': value} for eid, value in self.progress.get(sid, {}).items()]
    def write_collection(self, sid, body, exists):
        self.writes.append(('collection', sid, copy.deepcopy(body), exists))
        self.collections.setdefault(sid, {'subject_id': sid, 'subject_type': 2, 'tags': ['保留'], 'rate': 9, 'comment': '原短评', 'private': False}).update(body)
        if self.timeout_after_collection_write:
            self.timeout_after_collection_write = False
            raise BangumiError('测试网络超时')
    def write_episodes(self, sid, ids, state):
        self.writes.append(('episodes', sid, ids.copy(), state))
        self.progress.setdefault(sid, {}).update({eid: state for eid in ids})
        if self.timeout_after_episode_write:
            self.timeout_after_episode_write = False
            raise BangumiError('测试网络超时')


@pytest.fixture
def setup(tmp_path):
    db = Store(tmp_path / 'test.db')
    db.upsert_subject({'id': 42, 'name': 'Example', 'total_episodes': 2})
    db.execute("UPDATE shows SET selected=1,state='watching' WHERE id=42")
    remote, vault = FakeBangumi(), MemoryVault()
    sync = BangumiSync(db, vault, lambda token: remote)
    sync.connect('fake-test-token-only')
    sync.configure({'enabled': True})
    return db, remote, sync


@pytest.mark.parametrize('state,expected', [('wish', 1), ('trial', 3), ('watching', 3), ('completed', 2), ('paused', 4), ('dropped', 5)])
def test_status_mapping_never_changes_local_download_or_watch_state(setup, state, expected):
    db, remote, sync = setup
    db.execute('UPDATE shows SET state=?,cleanup_hold=1 WHERE id=42', (state,))
    before = db.show(42)
    sync.tick()
    assert remote.collections[42]['type'] == expected
    assert remote.collections[42]['private'] is True
    assert db.show(42) == before
    assert not db.rows('SELECT * FROM watches')
    assert not db.rows('SELECT * FROM tasks')


def test_explicit_watches_and_ids_not_watched_count_download_or_play_position(setup):
    db, remote, sync = setup
    db.execute("INSERT INTO watches(show_id,episode,finished,position,method) VALUES(42,1,1,0,'dandan')")
    db.execute("INSERT INTO watches(show_id,episode,finished,position,method) VALUES(42,2,0,500,'manual')")
    db.execute("INSERT INTO episodes(show_id,episode,status) VALUES(42,2,'complete')")
    db.execute('UPDATE shows SET watched=999 WHERE id=42')
    sync.tick()
    assert remote.progress[42] == {101: 2}
    assert remote.writes[0][2] == {'type': 3, 'private': True}
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'synced'
    assert all('ep_status' not in w[2] for w in remote.writes if w[0] == 'collection')
    revision = db.one('SELECT revision FROM bangumi_sync_queue')['revision']
    db.execute('UPDATE watches SET updated=123,position=600 WHERE show_id=42')
    assert db.one('SELECT revision FROM bangumi_sync_queue')['revision'] == revision


def test_catalog_default_wish_and_automatic_download_are_outside_sync(setup):
    db, remote, sync = setup
    db.upsert_subject({'id': 43, 'name': 'Default wish'})
    db.execute('UPDATE shows SET authorized=1,auto_download=1 WHERE id=43')
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(43,1,1,'dandan')")
    sync.tick()
    assert 43 not in remote.collections
    assert sync.status()['scope_count'] == 1


def test_atomic_queue_survives_restart_and_rollback_and_is_disabled_by_default(tmp_path):
    db = Store(tmp_path / 'test.db')
    db.upsert_subject({'id': 42, 'name': 'Example'})
    with pytest.raises(RuntimeError), db.connect() as c:
        c.execute('UPDATE shows SET selected=1 WHERE id=42')
        raise RuntimeError('transaction rollback')
    assert not db.rows('SELECT * FROM bangumi_sync_queue')
    db.execute('UPDATE shows SET selected=1 WHERE id=42')
    sync = BangumiSync(Store(db.path), MemoryVault(), Mock())
    sync.tick()
    assert sync.status()['counts']['pending'] == 1
    assert not sync.client_factory.called


def test_read_before_retry_does_not_repeat_successful_timed_out_episode_write(setup):
    db, remote, sync = setup
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(42,1,1,'manual')")
    remote.timeout_after_episode_write = True
    sync.tick()
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'error'
    restarted = BangumiSync(Store(db.path), sync.vault, lambda token: remote)
    restarted.retry()
    restarted.tick()
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'synced'
    assert len([w for w in remote.writes if w[0] == 'episodes']) == 1


def test_manual_undo_after_uncertain_episode_write_uses_durable_intent(setup):
    db, remote, sync = setup
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(42,1,1,'manual')")
    remote.timeout_after_episode_write = True
    sync.tick()
    db.execute('UPDATE watches SET finished=0 WHERE show_id=42 AND episode=1')
    sync.tick()
    assert remote.progress[42][101] == 0
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'synced'


def test_manual_undo_preserves_remote_only_progress(setup):
    db, remote, sync = setup
    remote.progress[42] = {102: 2}
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(42,1,1,'manual')")
    sync.tick()
    db.execute('UPDATE watches SET finished=0 WHERE show_id=42 AND episode=1')
    sync.tick()
    assert remote.progress[42] == {101: 0, 102: 2}


def test_preserve_remote_privacy_tags_rating_comment_and_private_drop_reason(setup):
    db, remote, sync = setup
    remote.collections[42] = {'subject_id': 42, 'subject_type': 2, 'type': 1, 'private': False,
                              'rate': 9, 'comment': '旧短评', 'tags': ['原标签']}
    db.execute("UPDATE shows SET rating=7,feedback='本地短评',drop_reason='私人原因' WHERE id=42")
    sync.tick()
    assert remote.collections[42] == {'subject_id': 42, 'subject_type': 2, 'type': 3, 'private': False,
                                       'rate': 9, 'comment': '旧短评', 'tags': ['原标签']}
    sync.configure({'ratings': True, 'comments': True})
    sync.tick()
    assert remote.collections[42]['rate'] == 7 and remote.collections[42]['comment'] == '本地短评'
    db.execute("UPDATE shows SET rating=NULL,feedback='' WHERE id=42")
    sync.tick()
    assert remote.collections[42]['rate'] == 0 and remote.collections[42]['comment'] == ''
    assert '私人原因' not in json.dumps(remote.writes, ensure_ascii=False)


def test_external_edits_conflict_and_only_explicit_resolution_overwrites(setup):
    db, remote, sync = setup
    sync.tick()
    remote.collections[42]['type'] = 4
    db.execute("UPDATE shows SET state='dropped' WHERE id=42")
    sync.tick()
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'conflict'
    assert remote.collections[42]['type'] == 4
    sync.retry()
    sync.tick()
    assert remote.collections[42]['type'] == 4
    sync.retry(42, resolve=True)
    sync.tick()
    assert remote.collections[42]['type'] == 5


def test_rating_correction_after_uncertain_write_and_disabled_interval(setup):
    db, remote, sync = setup
    sync.configure({'ratings': True, 'comments': True})
    db.execute("UPDATE shows SET rating=7,feedback='测试短评' WHERE id=42")
    remote.timeout_after_collection_write = True
    sync.tick()
    db.execute("UPDATE shows SET rating=NULL,feedback='' WHERE id=42")
    sync.tick()
    assert remote.collections[42]['rate'] == 0 and remote.collections[42]['comment'] == ''
    db.execute("UPDATE shows SET rating=8,feedback='短评2' WHERE id=42")
    sync.tick()
    sync.configure({'ratings': False, 'comments': False})
    sync.tick()
    db.execute("UPDATE shows SET rating=NULL,feedback='' WHERE id=42")
    sync.tick()
    assert remote.collections[42]['rate'] == 8 and remote.collections[42]['comment'] == '短评2'
    sync.configure({'ratings': True, 'comments': True})
    sync.tick()
    assert remote.collections[42]['rate'] == 0 and remote.collections[42]['comment'] == ''


def test_remote_removed_collection_is_not_recreated_without_confirmation(setup):
    db, remote, sync = setup
    sync.tick()
    del remote.collections[42]
    db.execute("UPDATE shows SET state='paused' WHERE id=42")
    sync.tick()
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'conflict'
    assert not remote.collections


def test_revision_change_during_read_prevents_stale_write(setup):
    db, remote, sync = setup
    remote.on_read = lambda: db.execute("UPDATE shows SET state='dropped' WHERE id=42")
    sync.tick()
    assert not remote.writes
    sync.tick()
    assert remote.collections[42]['type'] == 5


def test_missing_chapter_blocks_progress_without_guessing_or_losing_status(setup):
    db, remote, sync = setup
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(42,3,1,'manual')")
    sync.tick()
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'blocked'
    assert remote.collections[42]['type'] == 3
    assert not remote.progress


def test_newly_watched_chapter_refreshes_stale_catalog_immediately(setup):
    db, remote, sync = setup
    db.set('bangumi_episodes_42', {'time': time.time(), 'items': remote.numbering})
    remote.numbering = remote.numbering + [{'id': 103, 'type': 0, 'sort': 51, 'ep': 3}]
    db.execute("INSERT INTO watches(show_id,episode,finished,method) VALUES(42,3,1,'manual')")
    sync.tick()
    assert remote.progress[42][103] == 2
    assert db.one('SELECT state FROM bangumi_sync_queue')['state'] == 'synced'


@pytest.mark.parametrize('status', [401, 403, 429])
def test_auth_failure_and_rate_limit_stop_the_batch(setup, status):
    db, remote, sync = setup
    remote.fail = BangumiError('安全的状态消息', status, retry_after=120)
    sync.tick()
    assert len(remote.calls) == 1 and not remote.writes
    sync.tick()
    assert len(remote.calls) == 1
    assert sync.status()['health']['auth_required'] == (status != 429)


def test_account_replacement_requires_disconnect_and_receipts_are_per_account(setup):
    db, remote, sync = setup
    sync.tick()
    remote.user['id'] = 78
    with pytest.raises(ValueError, match='另一个'):
        sync.connect('fake-another-token')
    sync.disconnect()
    sync.connect('fake-another-token')
    assert not sync.options()['enabled']
    assert db.one('SELECT account_id FROM bangumi_sync_receipts')['account_id'] == 77


@pytest.mark.parametrize('mapping,rows,expected', [
    ({'offset': 48}, [{'id': 101, 'type': 0, 'sort': 49, 'ep': 1}], {1: 101}),
    ({'airing_offset': 77, 'offset': 11}, [{'id': 101, 'type': 0, 'sort': 78, 'ep': 1}], {1: 101}),
    ({'airing_offset': 0}, [{'id': 101, 'type': 0, 'sort': 1, 'ep': 49}], {1: 101}),
])
def test_only_bangumi_numbering_maps_chapters(tmp_path, mapping, rows, expected):
    db = Store(tmp_path / 'test.db'); db.upsert_subject({'id': 42, 'name': 'Example'})
    db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    assert episode_map(db, 42, rows) == expected


def test_ambiguous_chapter_mapping_is_rejected(tmp_path):
    db = Store(tmp_path / 'test.db'); db.upsert_subject({'id': 42, 'name': 'Example'})
    with pytest.raises(ValueError, match='含糊'):
        episode_map(db, 42, [{'id': 101, 'type': 0, 'ep': 1}, {'id': 102, 'type': 0, 'ep': 1}])
    with pytest.raises(ValueError, match='归属'):
        episode_map(db, 42, [{'id': 101, 'type': 0, 'ep': 1, 'subject_id': 43}])
    with pytest.raises(ValueError, match='季内编号'):
        episode_map(db, 42, [{'id': 101, 'type': 0, 'sort': 49}])
    db.set('airing_sort_offset_42', 0)
    with pytest.raises(ValueError, match='季内编号'):
        episode_map(db, 42, [{'id': 101, 'type': 0, 'sort': 49}])


@pytest.mark.parametrize('basis,expected', [('sort', {13: 101, 14: 102}), ('ep', {1: 101, 2: 102})])
def test_audited_sync_numbering_preserves_download_mapping_and_watches(setup, basis, expected):
    db, remote, sync = setup
    db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps({'airing_offset': 0, 'offset': 77}),))
    rows = [{'id': 101, 'subject_id': 42, 'type': 0, 'sort': 13, 'ep': 1},
            {'id': 102, 'subject_id': 42, 'type': 0, 'sort': 14, 'ep': 2}]
    proof = {'subject_id': 42, 'basis': basis, 'sort_offset': 12,
             'anchor': {'id': 101, 'sort': 13, 'ep': 1}, 'evidence': ['https://official.example/episodes']}
    db.set('bangumi_sync_numbering_42', proof)
    local = 13 if basis == 'sort' else 1
    db.execute('INSERT INTO watches(show_id,episode,finished,method) VALUES(42,?,1,?)', (local, 'manual'))
    before = db.show(42), db.rows('SELECT * FROM watches')
    assert episode_map(db, 42, rows) == expected
    remote.numbering = rows
    sync.tick()
    assert remote.progress[42] == {101: 2}
    assert (db.show(42), db.rows('SELECT * FROM watches')) == before
    for key, value in [('subject_id', 43), ('anchor', {'id': 999, 'sort': 13, 'ep': 1}),
                       ('sort_offset', 0), ('evidence', []), ('basis', 'offset')]:
        db.set('bangumi_sync_numbering_42', {**proof, key: value})
        with pytest.raises(ValueError, match='证据不匹配'):
            episode_map(db, 42, rows)
    db.set('bangumi_sync_numbering_42', proof)
    for key, value in [('subject_id', 43), ('ep', 0), ('sort', 15)]:
        with pytest.raises(ValueError, match='证据不匹配'):
            episode_map(db, 42, [rows[0], {**rows[1], key: value}])


def test_http_client_uses_fixed_tls_host_bearer_header_and_does_not_follow_redirects():
    session = Mock()
    session.request.return_value = Mock(status_code=204)
    client = BangumiClient('fake-secret', session)
    client.write_episodes(42, [101], 2)
    args, kwargs = session.request.call_args
    assert args == ('PATCH', 'https://api.bgm.tv/v0/users/-/collections/42/episodes')
    assert kwargs['headers']['Authorization'] == 'Bearer fake-secret'
    assert not kwargs['allow_redirects']
    assert kwargs['json'] == {'episode_id': [101], 'type': 2}
    assert 'fake-secret' not in args[1]


def test_http_errors_never_expose_request_url_headers_or_response_secrets():
    session = Mock()
    session.request.side_effect = requests.ConnectionError('fake-secret?token=do-not-log')
    with pytest.raises(BangumiError) as error:
        BangumiClient('fake-secret', session).me()
    assert 'fake-secret' not in str(error.value) and 'do-not-log' not in str(error.value)


def test_http_202_collection_write_is_accepted_but_read_202_is_not_data():
    session = Mock()
    session.request.return_value = Mock(status_code=202, headers={})
    client = BangumiClient('fake-test-token-only', session)
    assert client.write_collection(42, {'type': 3}, exists=False) is None
    assert client.write_episodes(42, [101], 2) is None
    with pytest.raises(BangumiError) as error:
        client.me()
    assert error.value.status == 202


def test_client_pagination_advances_actual_length_and_checks_completeness():
    client = BangumiClient()
    client.request = Mock(side_effect=[{'total': 3, 'data': [{'id': 1}, {'id': 2}]}, {'total': 3, 'offset': 2, 'data': [{'id': 3}]}])
    assert [r['id'] for r in client.pages('/v0/episodes')] == [1, 2, 3]
    assert client.request.call_args.kwargs['params']['offset'] == 2
    client.request = Mock(side_effect=[{'total': 3, 'data': [{'id': 1}]}, {'total': 3, 'offset': 1, 'data': []}])
    with pytest.raises(BangumiError): client.pages('/v0/episodes')
    client.request = Mock(side_effect=[{'total': 3, 'data': [{'id': 1}]}, {'total': 1, 'offset': 1, 'data': []}])
    with pytest.raises(BangumiError): client.pages('/v0/episodes')


def test_catalog_episode_cache_keeps_ids_and_uses_actual_page_length(tmp_path):
    from urllib.parse import parse_qs, urlparse
    from anime.sources import Catalog
    db = Store(tmp_path / 'test.db'); web = Mock(); offsets = []
    def fetch(url):
        if '/subjects/' in url: return Mock(json=lambda: {'id': 42, 'name': 'Example', 'type': 2})
        offset = int(parse_qs(urlparse(url).query)['offset'][0]); offsets.append(offset)
        return Mock(json=lambda: {'total': 3, 'offset': offset, 'data': [
            {'id': 100 + n, 'type': 0, 'sort': n + 48, 'ep': n} for n in range(1, 4)[offset:offset + 2]]})
    web.fetch.side_effect = fetch
    Catalog(db, web).details(42)
    assert offsets == [0, 2]
    assert [r['id'] for r in db.get('bangumi_episodes_42')['items']] == [101, 102, 103]
    assert db.get('airing_sort_offset_42') == 48


def test_local_only_routes_csrf_validation_and_no_credential_echo(tmp_path, monkeypatch):
    monkeypatch.setattr('anime.app.load_deployment', lambda: {'public_authorities': [], 'lan_authority': '192.168.1.10:4871', 'lan_network': '192.168.1.0/24'})
    db = Store(tmp_path / 'test.db'); engine = Mock()
    app = create_app(db, engine, False)
    remote, vault = FakeBangumi(), MemoryVault()
    app.state.bangumi_sync.vault = vault
    app.state.bangumi_sync.client_factory = lambda token: remote
    client = TestClient(app, base_url='http://127.0.0.1:4871')
    headers = {'X-Anime-Token': client.get('/api/bootstrap').json()['token'], 'Origin': 'http://127.0.0.1:4871'}
    assert client.post('/api/bangumi-sync/connect', json={'access_token': 'fake-secret-token'}).status_code == 403
    assert client.post('/api/bangumi-sync/connect', headers={**headers, 'Origin': 'https://evil.test'}, json={'access_token': 'fake-secret-token'}).status_code == 403
    response = client.post('/api/bangumi-sync/connect', headers=headers, json={'access_token': 'fake-secret-token'})
    assert response.status_code == 200
    assert 'fake-secret-token' not in response.text
    assert 'fake-secret-token' not in client.get('/api/bangumi-sync').text
    assert 'fake-secret-token' not in json.dumps(db.rows('SELECT * FROM kv'))
    assert client.post('/api/bangumi-sync/retry', headers=headers, json={'resolve': True, 'show_id': 42}).status_code == 400
    assert client.post('/api/bangumi-sync/settings', headers=headers, json={'enabled': 'yes'}).status_code == 400
    lan = TestClient(app, base_url='http://192.168.1.10:4871', client=('192.168.1.20', 1234))
    assert lan.post('/api/bangumi-sync/connect', headers={'X-Anime-Token': headers['X-Anime-Token'], 'Origin': 'http://192.168.1.10:4871'}, json={'access_token': 'fake-secret-token'}).status_code == 403
    assert not engine.mock_calls


def test_windows_token_vault_roundtrip_is_encrypted(tmp_path):
    import os
    if os.name != 'nt': pytest.skip('DPAPI is Windows-only')
    vault = TokenVault(tmp_path)
    secret = {'id': 77, 'username': 'test-only', 'token': 'fake-test-secret-no-production-token'}
    vault.save(secret)
    assert b'fake-test-secret' not in vault.path.read_bytes()
    assert vault.read() == secret
    vault.clear()
    assert vault.read() is None
