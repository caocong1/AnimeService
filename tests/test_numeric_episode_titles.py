import pytest

from anime.releases import Rejected, select_files
from test_reliability import setup, torrent


MAPPING = {'aliases': ['Cyberpunk Edgerunners'], 'season': 2, 'require_season': True}
NAME = 'Cyberpunk.Edgerunners.S02E03.021.1080p.NF.WEB-DL.mkv'
FILES = [{'index': 0, 'name': NAME, 'size': 100}]


def test_numeric_title_needs_separate_matching_catalogue_evidence():
    with pytest.raises(Rejected):
        select_files(FILES, MAPPING, [3], [3])
    selected, ids = select_files(FILES, MAPPING, [3], [3], episode_titles={3: ['0+2=1']})
    assert selected == {3: FILES[0]}
    assert ids == [0]


@pytest.mark.parametrize('name,titles', [
    (NAME, {2: ['0+2=1']}),
    (NAME, {3: ['A different title']}),
    (NAME.replace('S02', 'S01'), {3: ['0+2=1']}),
    (NAME.replace('.021.', '.5.'), {3: ['0+2=1']}),
    (NAME.replace('Cyberpunk.Edgerunners', 'Other.Show'), {3: ['0+2=1']}),
    (NAME.replace('.1080p.', '.OVA.1080p.'), {3: ['0+2=1']}),
])
def test_numeric_title_evidence_cannot_relax_identity_or_fractional_gates(name, titles):
    with pytest.raises(Rejected):
        select_files([dict(FILES[0], name=name)], MAPPING, [3], [3], episode_titles=titles)


def test_numeric_title_still_requires_the_authorized_title_episode_range():
    with pytest.raises(Rejected):
        select_files(FILES, MAPPING, [3], [1], episode_titles={3: ['0+2=1']})


def test_explicit_clean_material_reuses_existing_engine_and_preserves_quality_gate(setup):
    import json
    db, qb, engine, candidate = setup
    db.set('viewing_workflow_policy', {'version': 1, 'enabled': True})
    db.set('subtitle_studio_enabled', True)
    db.set('subtitle_clean_media_preferred', True)
    db.execute('INSERT INTO source_policies VALUES(42,?,0)', (json.dumps({'mode': 'subtitle_only'}),))
    item = candidate('[Group] Example S01E01 [1080p][MultiSubs]')
    engine.ingest(item)
    assert qb.adds == 0
    engine.ingest(item, explicit_material=True, verified_torrent=torrent())
    assert qb.adds == 1
    assert db.one('SELECT status FROM tasks')['status'] == 'downloading'
    assert not db.rows('SELECT * FROM watches')
    engine.ingest(item, explicit_material=True, verified_torrent=torrent())
    assert qb.adds == 1


@pytest.mark.parametrize('block', ['watched', 'unselected', 'unauthorized', 'wrong_season', 'hard_subs'])
def test_explicit_material_keeps_scope_and_clean_media_gates(setup, block):
    import json
    db, qb, engine, candidate = setup
    db.set('viewing_workflow_policy', {'version': 1, 'enabled': True})
    db.set('subtitle_studio_enabled', True)
    db.set('subtitle_clean_media_preferred', True)
    db.execute('INSERT INTO source_policies VALUES(42,?,0)', (json.dumps({'mode': 'subtitle_only'}),))
    item = candidate('[Group] Example S01E01 [1080p][MultiSubs]')
    if block == 'watched': db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    if block == 'unselected': db.execute('UPDATE shows SET selected=0')
    if block == 'unauthorized': db.execute('UPDATE shows SET authorized=0')
    if block == 'wrong_season': item = dict(item, title=item['title'].replace('S01', 'S02'))
    if block == 'hard_subs': item = dict(item, title=item['title'].replace('MultiSubs', 'Hardsubs'))
    engine.ingest(item, explicit_material=True, verified_torrent=torrent())
    assert qb.adds == 0
    assert not db.rows('SELECT * FROM tasks')


def test_material_api_binds_candidate_manifest_and_requires_existing_auth(setup):
    import json
    from fastapi.testclient import TestClient
    from anime.app import create_app
    from anime.releases import bdecode
    db, qb, engine, candidate = setup
    db.set('viewing_workflow_policy', {'version': 1, 'enabled': True})
    db.set('subtitle_studio_enabled', True)
    db.set('subtitle_clean_media_preferred', True)
    db.execute('INSERT INTO source_policies VALUES(42,?,0)', (json.dumps({'mode': 'subtitle_only'}),))
    db.execute("INSERT INTO sources(id,show_id,kind,url) VALUES(1,42,'mikan','https://mikanani.me/test')")
    item = candidate('[Group] Example S01E01 [1080p][MultiSubs]')
    client = TestClient(create_app(db=db, engine=engine, start_worker=False), base_url='http://127.0.0.1:4871')
    path = '/api/shows/42/acquire-material'
    payload = {'candidate_id': item['id'], 'episodes': [1], 'expected_hash': bdecode(torrent())[0]}
    assert client.post(path, json=payload).status_code == 403
    headers = {'Origin': 'http://127.0.0.1:4871', 'X-Anime-Token': client.get('/api/bootstrap').json()['token']}
    assert client.post(path, headers=headers, json=dict(payload, expected_hash='0'*40)).status_code == 400
    assert client.post(path, headers=headers, json=dict(payload, episodes=[1,2])).status_code == 400
    assert qb.adds == 0
    result = client.post(path, headers=headers, json=payload)
    assert result.status_code == 200
    assert result.json()['task']['status'] == 'downloading'
    assert qb.adds == 1
    assert client.post(path, headers=headers, json=payload).status_code == 200
    assert qb.adds == 1


def test_numeric_title_selection_survives_downloader_dispatch_and_reconcile(setup):
    import json
    from test_reliability import encode
    db, qb, engine, candidate = setup
    mapping = db.mapping(42)
    mapping.update(season=2, require_season=True)
    db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    db.set('viewing_workflow_policy', {'version': 1, 'enabled': True})
    db.set('subtitle_studio_enabled', True)
    db.set('subtitle_clean_media_preferred', True)
    db.execute('INSERT INTO source_policies VALUES(42,?,0)', (json.dumps({'mode': 'subtitle_only'}),))
    db.execute("INSERT INTO airings(show_id,episode,title) VALUES(42,3,'0+2=1')")
    item = candidate('[Group] Example S02E01~03 [1080p][MultiSubs]')
    # Use a complete manifest for the declared range.
    data = encode({b'info': {b'name': b'Example', b'piece length': 16384, b'pieces': b'x'*20,
                           b'files': [{b'length': 100, b'path': [b'Example.S02E01.mkv']},
                                      {b'length': 100, b'path': [b'Example.S02E02.mkv']},
                                      {b'length': 100, b'path': [b'Example.S02E03.021.1080p.NF.WEB-DL.mkv']}]}})
    engine.ingest(item, explicit_material=True, verified_torrent=data)
    task = db.one('SELECT * FROM tasks')
    assert task['status'] == 'downloading'
    assert {f['index'] for f in qb.files(task['hash']) if f['priority'] > 0} == {0, 1, 2}
    engine.dispatch(task)
    assert qb.adds == 1
    assert {f['index'] for f in qb.files(task['hash']) if f['priority'] > 0} == {0, 1, 2}
