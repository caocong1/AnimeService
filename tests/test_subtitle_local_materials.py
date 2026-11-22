"""Exact local completion is neither a download receipt nor a season permission.

All files, including the canonical legacy qB backup, live in pytest's temporary
directory. The byte fixture crosses E1/E2 at a torrent piece boundary so a size
check or an E1-only hash cannot accidentally pass the completion proof.
"""
import copy
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from anime import subtitle_local_materials as local


SID = 548818
SEASON = 2


def bencode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, str):
        value = value.encode('utf-8')
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(bencode(v) for v in value) + b'e'
    if isinstance(value, dict):
        return b'd' + b''.join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b'e'
    raise TypeError(type(value))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return Path(path)


def ref(path):
    return {'path': str(Path(path).resolve()), 'sha256': sha(path)}


def rows(db, sql, args=()):
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, args)]


def execute(db, sql, args=()):
    with sqlite3.connect(db) as con:
        con.execute(sql, args)


def business_snapshot(db):
    with sqlite3.connect(db) as con:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='subtitle_local_materials' ORDER BY name")]
        return {table: list(con.execute('SELECT * FROM ' + table + ' ORDER BY rowid')) for table in tables}


@pytest.fixture
def material(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    private = data / 'subtitle-studio' / 'local-import-fixture'
    private.mkdir(parents=True)
    legacy = tmp_path / 'local-appdata'
    monkeypatch.setenv('LOCALAPPDATA', str(legacy))
    backup = legacy / 'qBittorrent' / 'BT_backup'
    backup.mkdir(parents=True)
    media_root = tmp_path / 'existing-downloads'
    package = '[Fixture] Medalist S2 [14-15]'
    names = ['[Fixture] Medalist [14].mkv', '[Fixture] Medalist [15].mkv']
    first, second = b'first-episode', b'second-file'
    (media_root / package).mkdir(parents=True)
    video = media_root / package / names[0]
    sibling = media_root / package / names[1]
    video.write_bytes(first)
    sibling.write_bytes(second)
    all_bytes = first + second
    piece_length = 8
    pieces = b''.join(hashlib.sha1(all_bytes[p:p + piece_length]).digest() for p in range(0, len(all_bytes), piece_length))
    info = {b'name': package.encode(), b'piece length': piece_length, b'pieces': pieces,
            b'files': [{b'length': len(content), b'path': [name.encode()]} for name, content in zip(names, [first, second])]}
    infohash = hashlib.sha1(bencode(info)).hexdigest()
    torrent = private / 'source.torrent'
    torrent.write_bytes(bencode({b'info': info}))
    canonical_torrent = backup / (infohash + '.torrent')
    canonical_torrent.write_bytes(torrent.read_bytes())
    resume = {b'file-format': b'libtorrent resume file', b'file-version': 1,
              b'info-hash': bytes.fromhex(infohash), b'paused': 1,
              b'completed_time': 1700000000, b'save_path': str(media_root).encode(),
              b'file_priority': [1, 1], b'pieces': b'\x01' * (len(pieces) // 20)}
    canonical_resume = backup / (infohash + '.fastresume')
    canonical_resume.write_bytes(bencode(resume))
    resume_copy = private / 'original.fastresume'
    resume_copy.write_bytes(canonical_resume.read_bytes())
    inventory = write_json(data / 'inventory.json', [
        {'path': str(video), 'size': len(first), 'mtime': video.stat().st_mtime,
         'verified_complete': True, 'legacy_active': False, 'completion_source': 'legacy_qbit'}])
    request = write_json(private / 'REQUEST.json', {
        'schema': 1, 'request': 'Synthetic exact E1 recovery fixture',
        'preparation_requested': [f'episode:{SID}:1']})
    existing = {'path': str(video), 'bytes': len(first), 'sha256': sha(video),
                'real_torrent_infohash': infohash, 'torrent_file_index': 0,
                'official_cumulative_episode': 14, 'verified_full_decode': True,
                'all_overlapping_pieces_matched': 2}
    handoff = write_json(private / 'HANDOFF.json', {
        'schema': 'subtitle-exact-e1-entry-engineering-handoff/v1',
        'jobs': [{'show_id': SID, 'season': SEASON, 'episode': 1, 'existing_media': existing}]})
    authority = write_json(private / 'authority.json', {
        'schema': 'subtitle-exact-local-material-authority/v1',
        'action': 'register_existing_local_material', 'scope': 'subtitle_only_no_download',
        'show_id': SID, 'season': SEASON, 'episode': 1,
        'request': ref(request), 'handoff': ref(handoff)})
    provenance = write_json(private / 'inspection.json', {'role': 'synthetic-local-completion-evidence'})
    mapping = {'aliases': ['Medalist S2', 'Medalist'], 'season': SEASON,
               'offset': 0, 'start': 1, 'end': 1, 'confirmed': True,
               'inventory_checked': True, 'require_season': True,
               'numbering': [{'min': 1, 'max': 9, 'offset': 0},
                             {'min': 14, 'max': 22, 'offset': 13}]}
    source_title = '[Fixture] Medalist S2 [14-15][1080p][简繁日内封]'
    source_url = f'https://mikanani.me/Download/20260101/{infohash}.torrent'
    proposal = write_json(private / 'mapping-proposal.json', {
        'mapping': mapping, 'infohash': infohash, 'source_title': source_title,
        'source_url': source_url, 'authorization_bounds': [1, 1],
        'requested_selected_local_episodes': [1], 'ids': [0],
        'selected': {'1': {'index': 0, 'name': package + '/' + names[0], 'size': len(first)}}})
    bound_handoff = json.loads(handoff.read_text())
    bound_handoff['jobs'][0]['local_scope_proposal'] = str(proposal.resolve())
    bound_handoff['evidence'] = [ref(proposal)]
    write_json(handoff, bound_handoff)
    bound_authority = json.loads(authority.read_text())
    bound_authority['handoff'] = ref(handoff)
    write_json(authority, bound_authority)
    spec = {'schema': 'subtitle-local-material-request/v1', 'kind': 'local_completed_torrent',
            'show_id': SID, 'season': SEASON, 'episode': 1, 'expected_input': 'fixture-input',
            'media': {'path': str(video), 'bytes': len(first), 'sha256': sha(video)},
            'source': {'infohash': infohash, 'file_index': 0,
                       'title': source_title, 'url': source_url},
            'mapping': mapping, 'authority': ref(authority), 'torrent': ref(torrent),
            'mapping_proposal': ref(proposal), 'provenance': [ref(provenance)]}
    spec_path = write_json(private / 'spec.json', spec)
    db = data / 'anime.db'
    with sqlite3.connect(db) as con:
        con.executescript('''
          CREATE TABLE shows(id INTEGER PRIMARY KEY,title TEXT,original TEXT,state TEXT,selected INTEGER,
            authorized INTEGER,cleanup_hold INTEGER,mapping TEXT,total INTEGER,auto_download INTEGER DEFAULT 0);
          CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE source_policies(show_id INTEGER PRIMARY KEY,config TEXT,updated REAL);
          CREATE TABLE watches(show_id INTEGER,episode INTEGER,finished INTEGER,PRIMARY KEY(show_id,episode));
          CREATE TABLE tasks(hash TEXT PRIMARY KEY,show_id INTEGER,title TEXT,status TEXT,selection TEXT,
            save_path TEXT,provider TEXT,purpose TEXT,acknowledged INTEGER,external_id INTEGER,error TEXT,created REAL,updated REAL);
          CREATE TABLE thunder_queue(hash TEXT PRIMARY KEY,state TEXT,lease TEXT,committing INTEGER,expires REAL,action TEXT);
          CREATE TABLE episodes(show_id INTEGER,episode INTEGER,hash TEXT,file_index INTEGER,path TEXT,status TEXT,size INTEGER,PRIMARY KEY(show_id,episode));
          CREATE TABLE subtitle_delivery_jobs(id TEXT PRIMARY KEY,show_id INTEGER,episode INTEGER,kind TEXT,
            state TEXT,revision INTEGER,input_key TEXT,payload TEXT,accepted_build_id TEXT,lease_owner TEXT,
            lease_token TEXT,lease_expires REAL,dispatch_token TEXT,dispatch_state TEXT,thread_id TEXT,created REAL,updated REAL);
          CREATE TABLE subtitle_studio_jobs(show_id INTEGER,episode INTEGER,state TEXT,payload TEXT,error TEXT,updated REAL);
          CREATE TABLE subtitle_targets(id TEXT PRIMARY KEY,show_id INTEGER,episode INTEGER,season INTEGER,payload TEXT,created REAL);
          CREATE TABLE subtitle_target_heads(show_id INTEGER,episode INTEGER,target_id TEXT);
          CREATE TABLE subtitle_viewing_leases(show_id INTEGER,episode INTEGER,session TEXT,expires REAL);
          CREATE TABLE finished_viewing_leases(show_id INTEGER,episode INTEGER,session TEXT,expires REAL);
          CREATE TABLE source_samples(candidate_id INTEGER,show_id INTEGER,hash TEXT,state TEXT,path TEXT,evidence TEXT);
          CREATE TABLE subtitle_samples(candidate_id INTEGER,show_id INTEGER,episode INTEGER,hash TEXT,state TEXT,path TEXT,evidence TEXT);
        ''')
        saved_mapping = {**mapping, 'end': 9}
        con.execute('INSERT INTO shows VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (SID, '金牌得主 第二季', 'メダリスト 第2期', 'watching', 1, 0, 0, json.dumps(saved_mapping), 9, 0))
        con.execute('INSERT INTO subtitle_delivery_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (f'episode:{SID}:1', SID, 1, 'episode', 'blocked', 1, 'fixture-input', '{}', None,
                     '', '', 0, '', '', '', 1, 1))
    return SimpleNamespace(db=db, spec=spec, spec_path=spec_path, private=private,
                           media=video, sibling=sibling, root=media_root, infohash=infohash,
                           canonical_resume=canonical_resume, canonical_torrent=canonical_torrent,
                           resume=resume, torrent=torrent, info=info, authority=authority,
                           request=request, handoff=handoff, inventory=inventory, proposal=proposal)


def save_spec(fixture):
    return write_json(fixture.spec_path, fixture.spec)


def commit(fixture):
    return local.register(fixture.db, save_spec(fixture), apply=True)


def test_dry_run_opens_no_write_connection_and_does_not_initialize_tables(material, monkeypatch):
    before = material.db.read_bytes()
    connect = sqlite3.connect
    seen = []

    def readonly_connect(database, *args, **kwargs):
        seen.append(str(database))
        assert kwargs.get('uri') is True and 'mode=ro' in str(database)
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', readonly_connect)
    local.register(material.db, material.spec_path, apply=False)
    assert seen and material.db.read_bytes() == before


def test_valid_import_is_idempotent_and_changes_only_the_registry(material):
    before = business_snapshot(material.db)
    commit(material)
    registry = rows(material.db, 'SELECT * FROM subtitle_local_materials')
    commit(material)
    assert rows(material.db, 'SELECT * FROM subtitle_local_materials') == registry
    assert len(registry) == 1 and business_snapshot(material.db) == before
    assert local.allowed(material.db, SID, 1) is True
    assert local.allowed(material.db, SID, 2) is False
    assert rows(material.db, 'SELECT authorized,mapping FROM shows')[0]['authorized'] == 0
    assert not rows(material.db, 'SELECT * FROM tasks')


def test_material_is_explicit_provenance_not_a_task(material):
    commit(material)
    item = local.get_material(material.db, SID, 1)
    assert item['kind'] == 'local_completed_torrent'
    assert item['source_infohash'] == material.infohash
    assert Path(item['path']) == material.media.resolve()
    assert item['sha256'] == sha(material.media)
    assert item['selection']['ids'] == [0]
    assert set(item['selection']['selected']) == {'1'}
    assert 'provider' not in item and 'acknowledged' not in item and 'task_hash' not in item
    assert local.get_material_by_id(material.db, item['receipt_id']) == item
    assert local.revision(material.db, SID, 1)


@pytest.mark.parametrize('field,value', [('episode', 2), ('season', 1), ('show_id', 707667), ('kind', 'youtube')])
def test_wrong_identity_or_kind_is_refused_without_writes(material, field, value):
    material.spec[field] = value
    before = business_snapshot(material.db)
    with pytest.raises(ValueError):
        commit(material)
    assert business_snapshot(material.db) == before


@pytest.mark.parametrize('change', ['missing_media', 'source_list', 'mapping_list'])
def test_malformed_request_objects_report_value_error(material, change):
    if change == 'missing_media':
        del material.spec['media']
    elif change == 'source_list':
        material.spec['source'] = []
    else:
        material.spec['mapping'] = []
    with pytest.raises(ValueError):
        commit(material)


def test_actual_preparing_job_state_can_import_unowned_material(material):
    execute(material.db, "UPDATE subtitle_delivery_jobs SET state='preparing'")
    commit(material)
    assert local.allowed(material.db, SID, 1) is True


@pytest.mark.parametrize('change', ['file_index', 'media_sha', 'media_bytes', 'wide_scope', 'request_not_e1', 'authority_hash', 'torrent_hash'])
def test_unbound_source_or_scope_proof_is_refused(material, change):
    if change == 'file_index':
        material.spec['source']['file_index'] = 1
    elif change == 'media_sha':
        material.spec['media']['sha256'] = '0' * 64
    elif change == 'media_bytes':
        material.spec['media']['bytes'] += 1
    elif change == 'wide_scope':
        material.spec['mapping']['end'] = 9
    elif change == 'request_not_e1':
        request = json.loads(material.request.read_text())
        request['preparation_requested'] = [f'episode:{SID}:2']
        write_json(material.request, request)
        authority = json.loads(material.authority.read_text())
        authority['request'] = ref(material.request)
        write_json(material.authority, authority)
        material.spec['authority'] = ref(material.authority)
    elif change == 'authority_hash':
        material.spec['authority']['sha256'] = '0' * 64
    else:
        material.spec['torrent']['sha256'] = '0' * 64
    with pytest.raises(ValueError):
        commit(material)


@pytest.mark.parametrize('change', ['caller_alias', 'source_url', 'unbound_proposal', 'missing_handoff_ref', 'wide_proposal'])
def test_original_mapping_proposal_binds_numbering_selection_and_source(material, change):
    if change == 'caller_alias':
        material.spec['mapping']['aliases'].append('Unrelated series')
    elif change == 'source_url':
        material.spec['source']['url'] = 'https://example.invalid/another-source.torrent'
    elif change in ('unbound_proposal', 'wide_proposal'):
        proposal = json.loads(material.proposal.read_text(encoding='utf-8'))
        if change == 'unbound_proposal':
            proposal['source_title'] += ' altered'
        else:
            proposal['authorization_bounds'] = [1, 9]
            proposal['requested_selected_local_episodes'] = [1, 2]
        write_json(material.proposal, proposal)
        material.spec['mapping_proposal'] = ref(material.proposal)
        if change == 'wide_proposal':
            handoff = json.loads(material.handoff.read_text())
            handoff['evidence'] = [ref(material.proposal)]
            write_json(material.handoff, handoff)
            authority = json.loads(material.authority.read_text())
            authority['handoff'] = ref(material.handoff)
            write_json(material.authority, authority)
            material.spec['authority'] = ref(material.authority)
    else:
        handoff = json.loads(material.handoff.read_text())
        handoff['evidence'] = []
        write_json(material.handoff, handoff)
        authority = json.loads(material.authority.read_text())
        authority['handoff'] = ref(material.handoff)
        write_json(material.authority, authority)
        material.spec['authority'] = ref(material.authority)
    with pytest.raises(ValueError):
        commit(material)


@pytest.mark.parametrize('change', ['running', 'not_completed', 'wrong_hash', 'priority_zero', 'mapped_files', 'missing_original'])
def test_unproven_or_active_original_download_completion_is_refused(material, change):
    resume = copy.deepcopy(material.resume)
    if change == 'running':
        resume[b'paused'] = 0
    elif change == 'not_completed':
        resume[b'completed_time'] = 0
    elif change == 'wrong_hash':
        resume[b'info-hash'] = b'\0' * 20
    elif change == 'priority_zero':
        resume[b'file_priority'][0] = 0
    elif change == 'mapped_files':
        resume[b'mapped_files'] = [b'elsewhere.mkv']
    else:
        material.canonical_torrent.unlink()
    material.canonical_resume.write_bytes(bencode(resume))
    with pytest.raises(ValueError):
        commit(material)


def test_piece_crossing_e1_e2_is_recomputed_without_authorizing_e2(material):
    # E1 itself and its full SHA remain unchanged; E2 supplies the final three
    # bytes of E1's last piece, so merely trusting E1 SHA must fail here.
    material.sibling.write_bytes(b'X' + material.sibling.read_bytes()[1:])
    with pytest.raises(ValueError):
        commit(material)
    assert rows(material.db, 'SELECT authorized FROM shows') == [{'authorized': 0}]


@pytest.mark.parametrize('sql,args', [
    ('UPDATE shows SET selected=0', ()),
    ("UPDATE shows SET state='paused'", ()),
    ('UPDATE shows SET cleanup_hold=1', ()),
    ('INSERT INTO watches VALUES(?,?,1)', (SID, 1)),
    ("UPDATE subtitle_delivery_jobs SET thread_id='owner',dispatch_state='acknowledged'", ()),
    ("UPDATE subtitle_delivery_jobs SET dispatch_state='unknown'", ()),
    ("UPDATE subtitle_delivery_jobs SET dispatch_state='committing'", ()),
    ("UPDATE subtitle_delivery_jobs SET lease_owner='owner',lease_expires=9999999999", ()),
    ("UPDATE subtitle_delivery_jobs SET input_key='stale'", ()),
    ("INSERT INTO subtitle_target_heads VALUES(?,1,'frozen')", (SID,)),
    ("INSERT INTO source_policies VALUES(?,'{\"mode\":\"hold\"}',1)", (SID,)),
])
def test_live_protections_and_expected_input_cannot_be_bypassed(material, sql, args):
    if 'subtitle_target_heads' in sql:
        execute(material.db, "INSERT INTO subtitle_targets VALUES('frozen',?,1,2,'{}',1)", (SID,))
    execute(material.db, sql, args)
    before = business_snapshot(material.db)
    with pytest.raises(ValueError):
        commit(material)
    assert business_snapshot(material.db) == before


@pytest.mark.parametrize('protection', ['foreign_task', 'unknown_queue', 'dialog_hold'])
def test_existing_hash_receipts_are_not_adopted_or_erased(material, protection):
    if protection == 'foreign_task':
        execute(material.db, "INSERT INTO tasks(hash,show_id,status,acknowledged,provider) VALUES(?,999,'uncertain',0,'qbit')", (material.infohash,))
    elif protection == 'unknown_queue':
        execute(material.db, "INSERT INTO thunder_queue(hash,state,lease,committing) VALUES(?,'review','',1)", (material.infohash,))
    else:
        execute(material.db, 'INSERT INTO kv VALUES(?,?)', ('thunder_dialog_hold_' + material.infohash, '{"unconfirmed":true}'))
    before = business_snapshot(material.db)
    with pytest.raises(ValueError):
        commit(material)
    assert business_snapshot(material.db) == before


def test_registered_media_is_invalidated_by_same_size_change(material):
    commit(material)
    old_stat = material.media.stat()
    material.media.write_bytes(b'X' + material.media.read_bytes()[1:])
    os.utime(material.media, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(ValueError):
        local.get_material(material.db, SID, 1, verify_media=True)


def test_scope_change_before_write_connection_is_rechecked_transactionally(material, monkeypatch):
    connect = sqlite3.connect
    altered = False

    def racing_connect(database, *args, **kwargs):
        nonlocal altered
        if not altered and 'mode=ro' not in str(database):
            altered = True
            with connect(material.db) as rival:
                rival.execute('UPDATE shows SET selected=0')
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', racing_connect)
    with pytest.raises(ValueError):
        commit(material)
    assert altered
    with connect(material.db) as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subtitle_local_materials'").fetchone()
        assert not exists or not con.execute('SELECT 1 FROM subtitle_local_materials').fetchone()


@pytest.mark.parametrize('change', ['input', 'watched', 'unknown'])
def test_protection_changed_after_first_byte_verification_aborts_commit(material, monkeypatch, change):
    verify = local._verify_media
    changed = False

    def verify_and_race(*args, **kwargs):
        nonlocal changed
        result = verify(*args, **kwargs)
        if not changed:
            changed = True
            if change == 'input':
                execute(material.db, "UPDATE subtitle_delivery_jobs SET input_key='rival-input'")
            elif change == 'watched':
                execute(material.db, 'INSERT INTO watches VALUES(?,1,1)', (SID,))
            else:
                execute(material.db, "UPDATE subtitle_delivery_jobs SET dispatch_state='unknown'")
        return result

    monkeypatch.setattr(local, '_verify_media', verify_and_race)
    with pytest.raises(ValueError):
        commit(material)
    assert changed
    assert not rows(material.db, "SELECT name FROM sqlite_master WHERE type='table' AND name='subtitle_local_materials'")


def test_same_size_media_replacement_after_first_verification_aborts_commit(material, monkeypatch):
    verify = local._verify_media
    changed = False

    def verify_and_replace(*args, **kwargs):
        nonlocal changed
        result = verify(*args, **kwargs)
        if not changed:
            changed = True
            old = material.media.stat()
            material.media.write_bytes(b'X' + material.media.read_bytes()[1:])
            os.utime(material.media, ns=(old.st_atime_ns, old.st_mtime_ns))
        return result

    monkeypatch.setattr(local, '_verify_media', verify_and_replace)
    with pytest.raises(ValueError):
        commit(material)
    assert changed
    assert not rows(material.db, "SELECT name FROM sqlite_master WHERE type='table' AND name='subtitle_local_materials'")


def test_existing_finished_delivery_is_preserved_and_blocks_import(material):
    key = f'subtitle_delivery_finished_{SID}_1'
    value = json.dumps({'release_id': 'existing-v0', 'delivery_status': 'finished', 'accepted': False})
    execute(material.db, 'INSERT INTO kv VALUES(?,?)', (key, value))
    before = business_snapshot(material.db)
    with pytest.raises(ValueError):
        commit(material)
    assert business_snapshot(material.db) == before


@pytest.mark.parametrize('binding', ['episode', 'registry', 'source_index'])
def test_cross_show_binding_of_the_same_path_is_not_adopted(material, binding):
    if binding == 'episode':
        execute(material.db, "INSERT INTO episodes VALUES(999,1,'foreign',0,?,'complete',?)", (str(material.media.resolve()), material.media.stat().st_size))
    else:
        prior_path = str(material.media.resolve()) if binding == 'registry' else str(material.private / 'another-copy.mkv')
        prior_request = {'media': {'path': prior_path},
                         'source': {'infohash': material.infohash, 'file_index': 0}}
        with sqlite3.connect(material.db) as con:
            con.execute('CREATE TABLE subtitle_local_materials(id TEXT PRIMARY KEY,show_id INTEGER,season INTEGER,episode INTEGER,payload TEXT,created REAL)')
            con.execute('INSERT INTO subtitle_local_materials VALUES(?,999,1,1,?,1)',
                        ('foreign-material', json.dumps({'request': prior_request})))
    before = business_snapshot(material.db)
    with pytest.raises(ValueError):
        commit(material)
    assert business_snapshot(material.db) == before


@pytest.mark.parametrize('change', ['hold', 'watched'])
def test_later_explicit_hold_or_watch_revokes_subtitle_scope(material, change):
    commit(material)
    if change == 'hold':
        execute(material.db, "INSERT INTO source_policies VALUES(?,'{\"mode\":\"hold\"}',1)", (SID,))
    else:
        execute(material.db, 'INSERT INTO watches VALUES(?,1,1)', (SID,))
    assert local.allowed(material.db, SID, 1) is False


def test_registry_payload_change_without_revision_proof_is_refused(material):
    commit(material)
    original = rows(material.db, 'SELECT * FROM subtitle_local_materials')[0]
    payload = json.loads(original['payload'])
    payload['request']['source']['infohash'] = '0' * 40
    execute(material.db, 'UPDATE subtitle_local_materials SET payload=?', (json.dumps(payload),))
    with pytest.raises(ValueError, match='integrity'):
        local.get_material_by_id(material.db, original['id'])
    assert local.allowed(material.db, SID, 1) is False


@pytest.mark.parametrize('table', ['source_samples', 'subtitle_samples'])
def test_other_show_sample_identity_cannot_be_adopted(material, table):
    execute(material.db, f'INSERT INTO {table}(candidate_id,show_id,hash,path) VALUES(1,999,?,?)',
            (material.infohash, str(material.media.resolve())))
    with pytest.raises(ValueError):
        commit(material)


def test_original_inventory_refresh_does_not_erase_archived_completion_evidence(material):
    commit(material)
    stored = json.loads(rows(material.db, 'SELECT payload FROM subtitle_local_materials')[0]['payload'])
    evidence_paths = [Path(p) for p in stored['evidence']]
    archived_inventory = [p for p in evidence_paths if p.name.endswith('.inventory.json')]
    assert len(archived_inventory) == 1
    assert json.loads(archived_inventory[0].read_text())[0]['verified_complete'] is True
    write_json(material.inventory, [])
    material.canonical_resume.unlink()
    assert local.get_material(material.db, SID, 1)['sha256'] == sha(material.media)


@pytest.mark.parametrize('field,value', [('verified_complete', False), ('legacy_active', True)])
def test_inventory_cannot_supply_incomplete_or_active_files(material, field, value):
    inventory = json.loads(material.inventory.read_text())
    inventory[0][field] = value
    write_json(material.inventory, inventory)
    with pytest.raises(ValueError):
        commit(material)


def test_normal_later_owner_does_not_invalidate_immutable_material_revision(material):
    commit(material)
    revision = local.revision(material.db, SID, 1)
    execute(material.db, "UPDATE subtitle_delivery_jobs SET thread_id='new-original-owner',state='production',dispatch_state='acknowledged',revision=revision+1")
    assert local.get_material(material.db, SID, 1)['source_infohash'] == material.infohash
    assert local.revision(material.db, SID, 1) == revision


def test_external_provenance_reference_is_not_accepted_as_private_evidence(material, tmp_path):
    outside = write_json(tmp_path / 'outside.json', {'untrusted': True})
    material.spec['provenance'] = [ref(outside)]
    with pytest.raises(ValueError):
        commit(material)
