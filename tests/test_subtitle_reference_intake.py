"""Synthetic same-owner evidence only; no network, actual downloader or real DB."""
import copy
import datetime
import hashlib
from html import escape
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from test_reliability import encode
from test_reliability import setup
from test_subtitle_delivery import delivery
from anime.engine import Engine
from anime.native_indexes import candidate_mapping, clean_blocker, task_clean_blocker
from anime.releases import Rejected, bdecode, select_files, validate_title
from anime.source_review import SourceReview, family
from anime.sources import Sources
from anime.subtitle_lab import lab_core
from anime.subtitle_reference_intake import (KEY, MAX_AGE, SCHEMA, canonical,
    reference_mapping, reference_only, reference_task_blocker, register_reference_candidate,
    require_reference_candidate)
from anime.subtitle_studio import SubtitleStudio


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def facts(db):
    return {r['name']: db.rows('SELECT * FROM ' + r['name'] + ' ORDER BY rowid')
            for r in db.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


@pytest.fixture
def material(delivery):
    db, scheduler, *_ = delivery
    db.set('subtitle_studio_enabled', True)
    db.set('subtitle_clean_media_preferred', True)
    db.set('minimum_free_gb', 0)
    mapping = {'aliases': ['作品第二季'], 'season': 2, 'offset': 0, 'require_season': True,
               'start': 1, 'end': 3, 'confirmed': True, 'inventory_checked': True}
    db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    db.execute("INSERT INTO airings VALUES(42,2,'2020-01-01','鍛冶王')")
    db.execute('''CREATE TABLE subtitle_samples(candidate_id INTEGER PRIMARY KEY,show_id INTEGER,episode INTEGER,
        family TEXT,hash TEXT,state TEXT,path TEXT DEFAULT '',size INTEGER DEFAULT 0,error TEXT DEFAULT '',
        updated REAL,evidence TEXT DEFAULT '{}')''')
    root = (db.path.parent / 'subtitle-studio').resolve()
    root.mkdir(exist_ok=True)
    mother = root / '[CR] Example S02E02.mkv'; mother.write_bytes(b'original CR bytes' * 10)
    mother_hash = 'd' * 40
    target = {'path': str(mother), 'size': mother.stat().st_size,
              'fingerprint': lab_core().fingerprint(mother), 'task_hash': mother_hash}
    selection = {'selected': {'2': {'index': 0, 'name': mother.name, 'size': mother.stat().st_size}},
                 'files': [{'index': 0, 'name': mother.name, 'size': mother.stat().st_size}], 'ids': [0]}
    db.execute('''INSERT INTO tasks(hash,show_id,title,status,selection,save_path,acknowledged,purpose)
        VALUES(?,42,?,'complete',?,?,1,'episode')''', (mother_hash, mother.name, json.dumps(selection), str(root)))
    original_input = 'b' * 64
    payload = {'owner_stopped_writing': True, 'user_stopped': False, 'recovery_class': 'repair_blocked',
               'assessment': {'input_signature': original_input, 'target_fingerprint': target['fingerprint'],
                              'material_refs': [{'kind': 'completed_media', 'target': target}]},
               'history': ['original failures are retained']}
    db.execute('''UPDATE subtitle_delivery_jobs SET state='blocked',dispatch_state='blocked-stopped',
        thread_id='original-owner',input_key=?,payload=?,lease_owner='retained-owner',lease_token='retained-token',
        dispatch_token='retained-dispatch',lease_expires=0 WHERE id='episode:42:2' ''',
        (original_input, json.dumps(payload)))
    db.set('subtitle_delivery_readiness_42_2', {'target': target})
    filename = '[Dynamis One] Example 2nd Season - 14 (ABEMA 1920x1080 AVC AAC MKV).mkv'
    torrent = encode({b'info': {b'name': filename.encode(), b'length': 100,
                              b'piece length': 16384, b'pieces': b'x' * 20}})
    h, files = bdecode(torrent)
    title = '[Group] 作品第二季 / Example 2nd Season - 14 (ABEMA 1920x1080 AVC AAC MKV)'
    detail = 'https://mikanani.me/Home/Episode/' + h
    torrent_url = 'https://mikanani.me/Download/20261028/' + h + '.torrent'
    work_url = 'https://mikanani.me/Home/Bangumi/2000'
    rss_url = 'https://mikanani.me/RSS/Bangumi?bangumiId=2000&subgroupid=615'
    nfo_url = 'https://nfo.example.invalid/info?html=1'; raw_url = 'https://nfo.example.invalid/info?raw=1'
    jp_url = 'https://jimaku.cc/entry/9/download/' + quote('Example.S02E14.鍛冶王.WEBRip.ABEMA.ja[cc].srt', safe='')
    nfo_text = ('General\nComplete name                            : ' + filename +
        '\nFormat                                   : Matroska\nVideo\nFormat : AVC\nText\n'
        'ID                                       : 3\nFormat                                   : ASS\n'
        'Codec ID                                 : S_TEXT/ASS\nLanguage                                 : Japanese\n'
        'Forced                                   : No\nCount of elements                        : 2\n')
    artifacts = {
        'mikan_work': (work_url, '<a href="https://bgm.tv/subject/42">Subject</a>'),
        'publisher': (detail, '<p>' + title + '</p><a href="https://bgm.tv/subject/42">Subject</a>'
            '<a href="/Home/Bangumi/2000#615">Work</a><a href="' + torrent_url + '">Torrent</a>'
            '<a href="' + escape(rss_url) + '">RSS</a><a href="' + escape(nfo_url) + '">MediaInfo</a>'),
        'rss': (rss_url, '<rss version="2.0"><channel><title>Fixture</title><link>' + work_url + '</link>'
            '<description>Fixture</description><item><title>' + escape(title) + '</title><link>' + detail +
            '</link><enclosure url="' + torrent_url + '" type="application/x-bittorrent" length="100"/></item></channel></rss>'),
        'torrent': (torrent_url, torrent),
        'nfo_html': (nfo_url, '<pre data-media-info-text>' + escape(nfo_text) + '</pre><a href="?raw=1">Raw</a>'),
        'nfo_text': (raw_url, nfo_text),
        'abema': ('https://abema.tv/video/episode/11-76_s2_p14', '<title>第2期</title><h1>第14話 鍛冶王</h1>'),
        'bangumi': ('https://api.bgm.tv/v0/episodes/9001', json.dumps({'id': 9001, 'subject_id': 42,
                    'ep': 2, 'sort': 14, 'name': '鍛冶王', 'airdate': '2020-01-01'})),
        'jimaku_page': ('https://jimaku.cc/entry/9', '<a href="' + jp_url.replace('https://jimaku.cc', '') + '">SRT</a>'),
        'japanese_srt': (None, '1\n00:01:31,257 --> 00:01:32,801\n実際の原稿\n\n2\n00:22:07,451 --> 00:22:08,993\n最後\n'),
    }
    evidence = {}
    for name, (url, value) in artifacts.items():
        path = root / (name + '.bin')
        path.write_bytes(value if isinstance(value, bytes) else value.encode('utf-8'))
        evidence[name] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if url: evidence[name]['url'] = url
    old_fetch = [evidence['japanese_srt'] | {'url': jp_url, 'bytes': len(artifacts['japanese_srt'][1].encode())}]
    evidence['japanese_fetch'] = save(root / 'old-fetch.json', old_fetch)
    import time
    now = time.time()
    req = {'schema': SCHEMA, 'show_id': 42, 'season': 2, 'episode': 2, 'external_episode': 14,
        'mikan_id': 2000, 'bangumi_episode_id': 9001, 'expected_owner': 'original-owner',
        'expected_input': original_input, 'saved_mapping_sha256': hashlib.sha256(canonical(mapping).encode()).hexdigest(),
        'target': target, 'expected_hash': h, 'files': files,
        'source': {'kind': 'mikan', 'url': rss_url, 'archive_url': work_url},
        'candidate': {'title': title, 'url': torrent_url, 'detail': detail},
        'japanese_source_url': jp_url, 'evidence': evidence}
    context = SimpleNamespace(db=db, request=req, path=root/'request.json', root=root, now=now,
        scheduler=scheduler, mother=mother, torrent=torrent, files=files)
    reseal(context)
    return context


def reseal(context):
    evidence = context.request['evidence']; fetches = []
    for ref in evidence.values():
        path = Path(ref['path'])
        if path.is_file(): ref['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        if ref.get('url'):
            fetches.append(ref | {'status': 200, 'bytes': path.stat().st_size,
                'checked_at': datetime.datetime.fromtimestamp(context.now, datetime.timezone.utc).isoformat()})
    evidence['fetches'] = save(context.root/'fetches.json', fetches)
    save(context.path, context.request)


def apply(context):
    return register_reference_candidate(context.db, context.path, apply=True, now=context.now)


def candidate(context, receipt):
    return context.db.one('SELECT * FROM candidates WHERE id=?', (receipt['candidate_id'],))


@pytest.mark.requires_lab
def test_dryrun_leaves_every_table_and_original_material_unchanged(material):
    c = material; before = facts(c.db); original = c.mother.read_bytes()
    result = register_reference_candidate(c.db, c.path, now=c.now)
    assert result['permitted_role'] == 'subtitle_reference' and not result['applied']
    assert result['selection']['selected'] == {'2': c.files[0]}
    assert not any(result[k] for k in ('download_submitted', 'owner_changed', 'target_changed',
        'saved_mapping_changed', 'ready', 'accepted', 'same_japanese_text_verified', 'same_clock_verified'))
    assert facts(c.db) == before and c.mother.read_bytes() == original
    assert not (c.root/'reference-intake-proofs').exists()


@pytest.mark.requires_lab
def test_apply_is_idempotent_and_changes_only_new_source_candidate_and_receipt(material):
    c = material; before = facts(c.db); old = c.mother.read_bytes()
    first = apply(c); second = apply(c)
    assert first['applied'] and second['idempotent'] and first['candidate_id'] == second['candidate_id']
    after = facts(c.db)
    for table in before:
        if table not in ('candidates', 'sources'): assert after[table] == before[table]
    assert len(after['candidates']) == len(before['candidates']) + 1
    assert len(after['sources']) == len(before['sources']) + 1
    assert len(after['subtitle_reference_intakes']) == 1
    current = candidate(c, first); mapping = candidate_mapping(c.db.mapping(42), current)
    assert validate_title(current['title'], mapping, {}, False) == [2]
    assert select_files(c.files, mapping, [2], [2]) == ({2: c.files[0]}, [0])
    assert mapping['aliases'][-1] == c.files[0]['name'] and mapping['require_season']
    assert 'numbering' not in c.db.mapping(42) and c.mother.read_bytes() == old
    require_reference_candidate(c.db, current, 'subtitle_reference', c.files, now=c.now)


@pytest.mark.requires_lab
@pytest.mark.parametrize('field', ['title', 'url', 'detail', 'hash', 'id', 'source_id', 'show_id'])
def test_mapping_rejects_any_candidate_identity_substitution(material, field):
    c = material; row = candidate(c, apply(c)); changed = dict(row)
    changed[field] = (changed[field] + 1) if isinstance(changed[field], int) else str(changed[field]) + '-other'
    with pytest.raises(ValueError): candidate_mapping(c.db.mapping(42), changed)


@pytest.mark.requires_lab
@pytest.mark.parametrize('external', [1, 2, 13, 15, 24])
def test_scoped_mapping_rejects_all_other_external_episodes(material, external):
    c = material; row = candidate(c, apply(c)); mapping = candidate_mapping(c.db.mapping(42), row)
    with pytest.raises(ValueError): validate_title(row['title'].replace(' - 14 ', f' - {external} '), mapping, {}, False)


@pytest.mark.requires_lab
def test_regular_rss_keeps_binding_and_exclusive_source_discards_other_episodes(material):
    c = material; result = apply(c); row = candidate(c, result)
    source = c.db.one('SELECT * FROM sources WHERE id=?', (result['source_id'],))
    item = c.request['candidate'] | {'source_metadata': {'fresh_field': 'retained'}}
    other = item | {'title': item['title'].replace(' - 14 ', ' - 15 '), 'url': item['url'] + '-other'}
    Sources(c.db).remember(source, [other, item])
    current = candidate(c, result)
    assert reference_only(current) and json.loads(current['source_metadata'])[KEY] == json.loads(row['source_metadata'])[KEY]
    assert json.loads(current['source_metadata'])['fresh_field'] == 'retained'
    assert len(c.db.rows('SELECT * FROM candidates')) == 1
    require_reference_candidate(c.db, current, 'subtitle_reference', now=c.now)


@pytest.mark.requires_lab
@pytest.mark.parametrize('conflict', ['title', 'detail', 'hash', 'hard', 'raw', 'binding'])
def test_conflicting_rss_never_erases_reference_only_or_unlocks_ordinary_use(material, conflict):
    c = material; result = apply(c); source = c.db.one('SELECT * FROM sources WHERE id=?', (result['source_id'],))
    item = copy.deepcopy(c.request['candidate']); item['source_metadata'] = {}
    if conflict in ('title', 'detail'): item[conflict] += '-changed'
    elif conflict == 'hash': item['source_metadata']['expected_hash'] = 'e' * 40
    elif conflict in ('hard', 'raw'): item['source_metadata']['media_mode'] = conflict
    else: item['source_metadata'][KEY] = {'unregistered': True}
    Sources(c.db).remember(source, [item]); row = candidate(c, result)
    assert reference_only(row) and clean_blocker(c.db, row)
    with pytest.raises(ValueError): candidate_mapping(c.db.mapping(42), row)
    with pytest.raises(ValueError): require_reference_candidate(c.db, row, 'subtitle_reference', now=c.now)
    Sources(c.db).remember(source, [c.request['candidate'] | {'source_metadata': {}}])
    assert json.loads(candidate(c, result)['source_metadata'])['reference_binding_conflict']


@pytest.mark.requires_lab
@pytest.mark.parametrize('change', ['watched', 'owner', 'input', 'lease', 'unknown', 'production', 'user_stop',
    'finished', 'unauthorized', 'unselected', 'hold', 'season', 'mapping', 'future', 'mother_bytes',
    'mother_target', 'mother_receipt', 'scope', 'reference_limit', 'space', 'budget', 'new_task', 'dialog'])
def test_live_guards_reject_without_changing_any_original_row(material, change, monkeypatch):
    c = material; db = c.db
    if change == 'watched': db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    elif change in ('owner', 'input', 'lease', 'unknown', 'production'):
        field, value = {'owner': ('thread_id', 'another-owner'), 'input': ('input_key', 'e' * 64),
            'lease': ('lease_expires', c.now + 100), 'unknown': ('dispatch_state', 'unknown'),
            'production': ('state', 'production')}[change]
        db.execute('UPDATE subtitle_delivery_jobs SET ' + field + '=? WHERE id=?', (value, 'episode:42:2'))
    elif change in ('user_stop', 'finished'):
        p = json.loads(db.one("SELECT payload FROM subtitle_delivery_jobs WHERE id='episode:42:2'")['payload'])
        p['user_stopped' if change == 'user_stop' else 'finished'] = True
        db.execute("UPDATE subtitle_delivery_jobs SET payload=? WHERE id='episode:42:2'", (json.dumps(p),))
    elif change == 'unauthorized': db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    elif change == 'unselected': db.execute('UPDATE shows SET selected=0 WHERE id=42')
    elif change == 'hold': db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    elif change in ('season', 'mapping', 'scope'):
        m = db.mapping(42); m.update({'season': 3} if change == 'season' else {'offset': 12} if change == 'mapping' else {'end': 1})
        db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(m),))
    elif change == 'future': db.execute("UPDATE airings SET airdate='2099-01-01' WHERE show_id=42 AND episode=2")
    elif change == 'mother_bytes': c.mother.write_bytes(b'changed original CR')
    elif change == 'mother_target': db.set('subtitle_delivery_readiness_42_2', {'target': {}})
    elif change == 'mother_receipt': db.set('thunder_dialog_hold_' + 'd' * 40, {'unconfirmed': True})
    elif change == 'reference_limit':
        for cid in (100, 101):
            db.execute('INSERT INTO subtitle_samples(candidate_id,show_id,episode,hash,evidence) VALUES(?,42,2,?,?)',
                (cid, str(cid), json.dumps({'workflow_role': 'subtitle_reference'})))
    elif change == 'space': monkeypatch.setattr('anime.subtitle_reference_intake.shutil.disk_usage', lambda _: SimpleNamespace(free=0))
    elif change == 'budget': db.execute('INSERT INTO subtitle_samples(candidate_id,show_id,episode,size) VALUES(100,42,2,?)', (20 * 1024**3,))
    elif change == 'new_task': db.execute("INSERT INTO tasks(hash,show_id,status) VALUES(?,42,'uncertain')", (c.request['expected_hash'],))
    elif change == 'dialog': db.set('thunder_dialog_hold_' + c.request['expected_hash'], {'unconfirmed': True})
    before = facts(db)
    with pytest.raises((ValueError, OSError)): apply(c)
    assert facts(db) == before


@pytest.mark.requires_lab
@pytest.mark.parametrize('change', ['sha', 'torrent_manifest', 'hash', 'title', 'new_season_title',
    'file', 'subject', 'catalogue_ep', 'catalogue_sort', 'abema', 'nfo', 'cue_count', 'jimaku',
    'expired', 'foreign_source', 'disabled_source', 'blacklist', 'fidelity'])
def test_evidence_and_source_identity_are_exact(material, change):
    c = material; r = c.request
    if change == 'sha': r['evidence']['torrent']['sha256'] = '0' * 64
    elif change == 'torrent_manifest': r['files'][0]['size'] += 1
    elif change == 'hash': r['expected_hash'] = 'e' * 40
    elif change == 'title': r['candidate']['title'] += ' altered'
    elif change == 'new_season_title': r['candidate']['title'] = r['candidate']['title'].replace('2nd Season', '3rd Season')
    elif change == 'file': r['files'][0]['name'] += '.different.mkv'
    elif change in ('subject', 'catalogue_ep', 'catalogue_sort'):
        p = Path(r['evidence']['bangumi']['path']); value = json.loads(p.read_text())
        value[{'subject':'subject_id', 'catalogue_ep':'ep', 'catalogue_sort':'sort'}[change]] += 1
        save(p, value); reseal(c)
    elif change == 'abema': Path(r['evidence']['abema']['path']).write_text('<h1>第14話 Another episode</h1>', encoding='utf-8'); reseal(c)
    elif change in ('nfo', 'cue_count'):
        p = Path(r['evidence']['nfo_text']['path'])
        p.write_text(p.read_text().replace('Japanese', 'English') if change == 'nfo' else p.read_text().replace(': 2\n', ': 3\n'), encoding='utf-8')
        reseal(c)
    elif change == 'jimaku': r['japanese_source_url'] = r['japanese_source_url'].replace('ABEMA', 'CR')
    elif change == 'expired': c.now += MAX_AGE + 61
    elif change in ('foreign_source', 'disabled_source'):
        c.db.execute('INSERT INTO sources(show_id,kind,url,archive_url,enabled) VALUES(?,?,?,?,?)',
            (43 if change == 'foreign_source' else 42, 'mikan', r['source']['url'], r['source']['archive_url'], 0 if change == 'disabled_source' else 1))
    elif change == 'blacklist': c.db.set('source_publisher_blacklist', [{'publisher': 'Dynamis One', 'aliases': ['Dynamis One'], 'reason': 'fixture'}])
    elif change == 'fidelity':
        c.db.set('source_fidelity', 'original'); r['candidate']['title'] += ' AI upscale'
    save(c.path, r); before = facts(c.db)
    with pytest.raises(ValueError): apply(c)
    assert facts(c.db) == before


@pytest.mark.requires_lab
def test_only_original_queue_can_create_exact_reference_sample_and_all_normal_routes_refuse(material, tmp_path):
    c = material; registered = apply(c); row = candidate(c, registered)
    before = facts(c.db)
    for explicit in (False, True):
        with pytest.raises(Rejected): Engine.ingest(SimpleNamespace(db=c.db), row, explicit_material=explicit)
    review = SourceReview.__new__(SourceReview); review.db = c.db
    with pytest.raises(ValueError): review.start(42, [row['id']])
    with pytest.raises(ValueError): review.queue(row)
    assert not review.decision(row, ordinary=True)['allowed']
    assert not SourceReview.filter_candidates([row], c.db.mapping(42), '', [])
    assert clean_blocker(c.db, row)
    with pytest.raises(ValueError): require_reference_candidate(c.db, row, 'clean_base', now=c.now)
    assert facts(c.db) == before
    engine = SimpleNamespace(db=c.db, sources=SimpleNamespace(torrent=Mock(return_value=c.torrent)),
        check_legacy_owner=Mock(), thunder=SimpleNamespace(reader=SimpleNamespace(by_hash=Mock(return_value=[]))),
        qb=SimpleNamespace(info=Mock(return_value=[])), incomplete=tmp_path/'incomplete',
        library=tmp_path/'library', cache=tmp_path/'cache', dispatch=Mock())
    engine.cache.mkdir(); studio = SubtitleStudio.__new__(SubtitleStudio); studio.db=c.db; studio.engine=engine
    studio.queue(row | {'episode': 2, 'family': family(row['title'])}, 'subtitle_reference')
    task = c.db.one('SELECT * FROM tasks WHERE hash=?', (registered['infohash'],))
    sample = c.db.one('SELECT * FROM subtitle_samples WHERE hash=?', (registered['infohash'],))
    assert task['purpose'] == 'subtitle_sample' and task['status'] == 'intent'
    assert json.loads(sample['evidence'])['workflow_role'] == 'subtitle_reference'
    assert not task_clean_blocker(c.db, task)
    for alteration in ({'purpose':'episode'}, {'selection':'{}'}, {'show_id':43}, {'title':'different'}):
        assert reference_task_blocker(c.db, task | alteration)
    from anime.subtitle_evidence import completed_selection
    path = Path(task['save_path']) / c.files[0]['name']; path.write_bytes(b'x' * 100)
    c.db.execute("UPDATE tasks SET status='complete',acknowledged=1 WHERE hash=?", (registered['infohash'],))
    assert completed_selection(c.db, 42, 2, path.resolve()) == (None, None)
    from anime.subtitle_targets import ProductionTargets
    targets = ProductionTargets.__new__(ProductionTargets); targets.db = c.db
    targets.studio = SimpleNamespace(ensure=Mock(), media=Mock(return_value=[{
        'show_id':42, 'episode':2, 'fingerprint':'reference-fp', 'hash':registered['infohash'],
        'path':str(path), 'size':100}]))
    with pytest.raises(ValueError, match='参考'):
        targets.eligible_media(42, 2, 'reference-fp')


@pytest.mark.requires_lab
def test_expired_or_tampered_registered_proofs_cannot_be_used_to_queue(material):
    c = material; row = candidate(c, apply(c)); before = facts(c.db)
    with pytest.raises(ValueError): require_reference_candidate(c.db, row, 'subtitle_reference', now=c.now + MAX_AGE + 61)
    stored = c.db.one('SELECT payload FROM subtitle_reference_intakes'); payload = json.loads(stored['payload'])
    Path(payload['archived_proofs'][0]['path']).write_bytes(b'changed immutable evidence')
    with pytest.raises(ValueError): require_reference_candidate(c.db, row, 'subtitle_reference', now=c.now)
    assert facts(c.db) == before


@pytest.mark.requires_lab
def test_default_cli_runs_without_initialization_or_mutation(material):
    import subprocess
    import sys
    c = material; before = facts(c.db)
    command = [sys.executable, str(Path(__file__).resolve().parents[1]/'scripts/register_subtitle_reference_candidate.py'),
               '--db', str(c.db.path), '--request', str(c.path)]
    result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['applied'] is False
    assert facts(c.db) == before and not (c.root/'reference-intake-proofs').exists()


@pytest.mark.requires_lab
def test_apply_rechecks_owner_inside_transaction(material, monkeypatch):
    import anime.subtitle_reference_intake as module
    c = material; original = module._load
    def raced(*args):
        result = original(*args)
        c.db.execute("UPDATE subtitle_delivery_jobs SET thread_id='different-owner' WHERE id='episode:42:2'")
        return result
    monkeypatch.setattr(module, '_load', raced)
    with pytest.raises(ValueError, match='owner'):
        apply(c)
    assert not c.db.rows('SELECT * FROM candidates')
    assert not c.db.one("SELECT name FROM sqlite_master WHERE name='subtitle_reference_intakes'")
    assert c.db.one("SELECT thread_id FROM subtitle_delivery_jobs WHERE id='episode:42:2'")['thread_id'] == 'different-owner'


@pytest.mark.requires_lab
def test_apply_rechecks_source_bytes_before_any_registration(material, monkeypatch):
    import anime.subtitle_reference_intake as module
    c = material; original = module._load; before = facts(c.db)
    def raced(*args):
        result = original(*args)
        Path(c.request['evidence']['japanese_srt']['path']).write_bytes(b'changed after load')
        return result
    monkeypatch.setattr(module, '_load', raced)
    with pytest.raises(ValueError, match='Source proof changed'):
        apply(c)
    assert facts(c.db) == before


@pytest.mark.requires_lab
def test_existing_exact_enabled_source_is_reused_without_modifying_it(material):
    c = material; source = c.request['source']
    cid = c.db.execute('INSERT INTO sources(show_id,kind,url,archive_url,enabled) VALUES(42,?,?,?,1)',
                      (source['kind'], source['url'], source['archive_url']))
    before = c.db.rows('SELECT * FROM sources')
    result = apply(c)
    assert result['source_id'] == cid and c.db.rows('SELECT * FROM sources') == before
    assert c.db.one('SELECT source_exclusive FROM subtitle_reference_intakes')['source_exclusive'] == 0


@pytest.mark.requires_lab
@pytest.mark.parametrize('metadata', ['', '{}', '{broken', '{"reference_only":false}'])
def test_durable_receipt_prevents_unmarking_a_reference(material, metadata):
    c = material; result = apply(c)
    c.db.execute('UPDATE candidates SET source_metadata=? WHERE id=?', (metadata, result['candidate_id']))
    row = candidate(c, result); before = facts(c.db)
    assert reference_only(row, c.db) and clean_blocker(c.db, row)
    with pytest.raises(Rejected): Engine.ingest(SimpleNamespace(db=c.db), row, explicit_material=True)
    review = SourceReview.__new__(SourceReview); review.db = c.db
    with pytest.raises(ValueError): review.start(42, [row['id']])
    assert not review.decision(row, ordinary=True)['allowed']
    with pytest.raises(ValueError): require_reference_candidate(c.db, row, 'subtitle_reference', now=c.now)
    assert facts(c.db) == before


@pytest.mark.requires_lab
def test_unregistered_metadata_cannot_create_a_reference_task(material):
    c = material; row = candidate(c, apply(c)); metadata = json.loads(row['source_metadata'])
    c.db.execute('DELETE FROM subtitle_reference_intakes')
    with pytest.raises(ValueError, match='registration'):
        require_reference_candidate(c.db, row, 'subtitle_reference', now=c.now)
    assert metadata[KEY] and not c.db.one('SELECT 1 FROM tasks WHERE hash=?', (c.request['expected_hash'],))


def test_regular_material_http_api_rejects_reference_before_torrent_or_reconcile(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db, qb, engine, make_candidate = setup
    row = make_candidate('[Group] Example S01E01 [1080p]')
    db.execute("INSERT INTO sources(id,show_id,kind,url,enabled) VALUES(1,42,'mikan','https://mikanani.me/test',1)")
    db.execute('UPDATE candidates SET source_metadata=? WHERE id=?', (json.dumps({'reference_only': True}), row['id']))
    engine.sources.torrent = Mock(side_effect=AssertionError('must refuse before any torrent fetch'))
    engine.reconcile = Mock(side_effect=AssertionError('must refuse before any reconciliation'))
    app = create_app(db=db, engine=engine, start_worker=False)
    client = TestClient(app, base_url='http://127.0.0.1:4871')
    headers = {'Origin':'http://127.0.0.1:4871', 'X-Anime-Token':client.get('/api/bootstrap').json()['token']}
    before_tasks = db.rows('SELECT * FROM tasks')
    response = client.post('/api/shows/42/acquire-material', headers=headers,
        json={'candidate_id':row['id'], 'episodes':[1], 'expected_hash':'e' * 40})
    assert response.status_code == 400 and 'subtitle_reference' in response.json()['error']
    assert db.rows('SELECT * FROM tasks') == before_tasks and qb.adds == 0
    engine.sources.torrent.assert_not_called(); engine.reconcile.assert_not_called()
