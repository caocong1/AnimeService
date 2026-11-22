"""Original synthetic declarations; no network, real DB or video submission."""
from contextlib import contextmanager
import hashlib
from html import escape
import json
from pathlib import Path
import time

import pytest

from test_reliability import encode
from test_subtitle_delivery import delivery
from anime.native_indexes import clean_blocker, media_mode
from anime.releases import bdecode
from anime.sources import Sources
from anime.subtitle_acquisition_declarations import register_acquisition_declaration


def ref(path):
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    return ref(path)


def fixture(delivery):
    db, scheduler, *_ = delivery
    db.set('subtitle_studio_enabled', True)
    db.set('subtitle_clean_media_preferred', True)
    mapping = db.mapping(42)
    mapping['aliases'] = ['Example']
    db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    root = (db.path.parent / 'subtitle-studio').resolve()
    root.mkdir(exist_ok=True)
    filename = '[Group] Example S01E02 [1080p].mkv'
    torrent = encode({b'info': {b'name': filename.encode(), b'length': 100,
                              b'piece length': 16384, b'pieces': b'x' * 20}})
    torrent_path = root / 'original.torrent'
    torrent_path.write_bytes(torrent)
    h, files = bdecode(torrent, include_padding=True)
    url = f'https://mikanani.me/Download/20261025/{h}.torrent'
    detail = f'https://mikanani.me/Home/Episode/{h}'
    sid = db.execute("INSERT INTO sources(show_id,kind,url,enabled) VALUES(42,'mikan','https://mikanani.me/RSS/Bangumi?bangumiId=1',1)")
    old_meta = json.dumps({'seeders': 18, 'declared_author': 'original fixture'})
    cid = db.execute('INSERT INTO candidates(show_id,source_id,title,url,detail,status,reason,source_metadata) VALUES(42,?,?,?,?,?,?,?)',
                     (sid, filename[:-4], url, detail, 'source_wait', 'original family wait', old_meta))
    candidate = db.one('SELECT * FROM candidates WHERE id=?', (cid,))
    source = db.one('SELECT * FROM sources WHERE id=?', (sid,))
    nfo_url = 'https://nfo.example.invalid/' + filename.replace(' ', '%20')
    page = root / 'publisher.html'
    page.write_text('<a href="' + nfo_url + '">MediaInfo</a>', encoding='utf-8')
    text = ('General\nComplete name                            : ' + filename +
            '\nFormat                                   : Matroska\n\nVideo\nFormat : HEVC\n\n')
    for i, language in enumerate(('English', 'Thai', 'Indonesian'), 1):
        text += (f'Text #{i}\nID                                       : {i+2}\n'
                 'Format                                   : ASS\n'
                 'Codec ID                                 : S_TEXT/ASS\n'
                 f'Language                                 : {language}\n'
                 'Forced                                   : No\n\n')
    raw = root / 'nfo.raw.txt'
    raw.write_text(text, encoding='utf-8')
    nfo = root / 'nfo.html'
    nfo.write_text('<pre data-media-info-text>' + escape(text) + '</pre>', encoding='utf-8')
    report = {'schema': 'subtitle-clean-acquisition-evidence/v1',
        'show_id': 42, 'season': 1, 'episode': 2, 'recommended_existing_candidate_id': cid,
        'existing_candidate_expected_hash': h, 'declaration_only': True, 'clean_frames_verified': False,
        'recommended_source_metadata_for_guarded_registration': {
            'media_mode': 'soft', 'declaration_only': True, 'expected_hash': h,
            'declaration_basis': 'Synthetic fixture: 3 independent publisher-declared ASS streams.',
            'declaration_page': ref(page) | {'url': detail}, 'declaration_mediainfo': ref(nfo),
            'extracted_mediainfo': ref(raw)},
        'actual_torrent_manifest': ref(torrent_path) | {'url': url, 'infohash': h, 'expected_hash': h, 'files': files}}
    report_path = root / 'report.json'
    request = {'schema': 'subtitle-acquisition-declaration-request/v1',
        'candidate_id': cid, 'show_id': 42, 'season': 1, 'episode': 2,
        'expected_input': scheduler.get('episode:42:2')['input_key'],
        'expected_metadata_sha256': hashlib.sha256(old_meta.encode()).hexdigest(),
        'candidate': {k: candidate[k] for k in ('title', 'url', 'detail', 'source_id', 'hash')},
        'source': {k: source[k] for k in ('id', 'kind', 'url')},
        'report': save(report_path, report), 'mediainfo_url': nfo_url}
    path = root / 'request.json'
    save(path, request)
    return db, path, request, report


def update_report(path, request, report):
    request['report'] = save(Path(request['report']['path']), report)
    save(path, request)


def facts(db):
    return {r['name']: db.rows('SELECT * FROM ' + r['name'] + ' ORDER BY rowid')
            for r in db.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


def test_default_plan_is_read_only_and_only_removes_the_projected_acquisition_blocker(delivery):
    db, path, request, _ = fixture(delivery)
    candidate = db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))
    before = facts(db)
    assert clean_blocker(db, candidate)
    result = register_acquisition_declaration(db, path)
    assert result['media_mode'] == 'soft' and result['declaration_only'] and not result['applied']
    assert not clean_blocker(db, candidate | {'source_metadata': result['projected_metadata']})
    assert len(result['declared_tracks']) == 3 and not any(result[k] for k in
        ('clean_frames_verified', 'ready', 'accepted', 'finished', 'download_submitted', 'metadata_changed'))
    assert facts(db) == before


def test_apply_changes_only_candidate_metadata_and_appends_original_evidence_history(delivery):
    db, path, request, _ = fixture(delivery)
    before = facts(db)
    candidate = db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))
    first = register_acquisition_declaration(db, path, apply=True)
    second = register_acquisition_declaration(db, path, apply=True)
    after = facts(db)
    current = db.one('SELECT * FROM candidates WHERE id=?', (candidate['id'],))
    meta = json.loads(current['source_metadata'])
    assert first['applied'] and first['metadata_changed'] and second['idempotent']
    assert not second['metadata_changed'] and first['id'] == second['id']
    assert {k:v for k,v in current.items() if k != 'source_metadata'} == {k:v for k,v in candidate.items() if k != 'source_metadata'}
    assert meta['seeders'] == 18 and meta['declared_author'] == 'original fixture'
    history = db.rows('SELECT * FROM subtitle_acquisition_declarations')
    assert len(history) == 1 and history[0]['metadata_before'] == candidate['source_metadata']
    assert json.loads(history[0]['payload'])['report']['clean_frames_verified'] is False
    for table in before:
        if table != 'candidates': assert before[table] == after[table]
    assert not clean_blocker(db, current)


def test_regular_rss_refresh_keeps_registered_declaration_and_new_feed_fields(delivery):
    db, path, request, _ = fixture(delivery)
    registered = register_acquisition_declaration(db, path, apply=True)
    source = db.one('SELECT * FROM sources WHERE id=?', (request['source']['id'],))
    item = request['candidate'] | {'source_metadata': {}}
    Sources(db).remember(source, [item])
    candidate = db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))
    assert media_mode(candidate) == 'soft'
    assert json.loads(candidate['source_metadata'])['acquisition_declaration']['id'] == registered['id']
    item['source_metadata'] = {'seeders': 99}
    Sources(db).remember(source, [item])
    assert json.loads(db.one('SELECT source_metadata FROM candidates')['source_metadata'])['seeders'] == 99
    assert len(db.rows('SELECT * FROM subtitle_acquisition_declarations')) == 1


@pytest.mark.parametrize('incoming', [{'media_mode': 'hard'}, {'expected_hash': '0' * 40}])
def test_conflicting_new_feed_evidence_is_not_overridden_by_the_old_soft_declaration(delivery, incoming):
    db, path, request, _ = fixture(delivery)
    register_acquisition_declaration(db, path, apply=True)
    source = db.one('SELECT * FROM sources WHERE id=?', (request['source']['id'],))
    Sources(db).remember(source, [request['candidate'] | {'source_metadata': incoming}])
    candidate = db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))
    assert media_mode(candidate) in ('hard', 'unknown') and clean_blocker(db, candidate)
    assert json.loads(candidate['source_metadata'])['acquisition_declaration_conflict']
    Sources(db).remember(source, [request['candidate'] | {'source_metadata': {}}])
    current = db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))
    assert media_mode(current) in ('hard', 'unknown') and clean_blocker(db, current)
    assert json.loads(current['source_metadata'])['acquisition_declaration_conflict']
    with pytest.raises(ValueError, match='Conflicting existing media-mode'):
        register_acquisition_declaration(db, path, apply=True)


@pytest.mark.parametrize('change', ['source_disabled', 'source_foreign_show', 'source_url', 'candidate_title',
    'candidate_url', 'candidate_detail', 'candidate_hash', 'metadata_changed', 'metadata_conflict',
    'hard_title', 'wrong_input', 'watched', 'unauthorized', 'owner', 'user_stopped', 'lease',
    'task', 'download_unknown', 'dialog_unknown', 'sample', 'blacklist'])
def test_protected_candidate_source_scope_and_receipts_are_rejected_without_writes(delivery, change):
    db, path, request, _ = fixture(delivery)
    cid = request['candidate_id']
    if change == 'source_disabled': db.execute('UPDATE sources SET enabled=0')
    elif change == 'source_foreign_show': db.execute('UPDATE sources SET show_id=43')
    elif change == 'source_url': db.execute("UPDATE sources SET url='https://example.invalid/changed'")
    elif change == 'candidate_title': db.execute("UPDATE candidates SET title='changed' WHERE id=?", (cid,))
    elif change == 'candidate_url': db.execute("UPDATE candidates SET url='https://example.invalid/changed.torrent' WHERE id=?", (cid,))
    elif change == 'candidate_detail': db.execute("UPDATE candidates SET detail='https://example.invalid/changed' WHERE id=?", (cid,))
    elif change == 'candidate_hash': db.execute('UPDATE candidates SET hash=? WHERE id=?', ('0' * 40, cid))
    elif change.startswith('metadata_'):
        db.execute('UPDATE candidates SET source_metadata=? WHERE id=?',
                   (json.dumps({'media_mode': 'hard'} if change == 'metadata_conflict' else {'new_fact': True}), cid))
    elif change == 'hard_title':
        title = request['candidate']['title'] + ' [Hardsub]'
        db.execute('UPDATE candidates SET title=? WHERE id=?', (title, cid))
        request['candidate']['title'] = title
        save(path, request)
    elif change == 'wrong_input':
        request['expected_input'] = 'wrong-live-input'
        save(path, request)
    elif change == 'watched': db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    elif change == 'unauthorized': db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    elif change == 'owner': db.execute("UPDATE subtitle_delivery_jobs SET thread_id='original-owner' WHERE id='episode:42:2'")
    elif change == 'user_stopped': db.execute("UPDATE subtitle_delivery_jobs SET payload=? WHERE id='episode:42:2'", (json.dumps({'user_stopped': True}),))
    elif change == 'lease': db.execute("UPDATE subtitle_delivery_jobs SET lease_expires=? WHERE id='episode:42:2'", (time.time() + 1000,))
    elif change == 'task': db.execute("INSERT INTO tasks(hash,show_id,status) VALUES(?,42,'complete')", (request['candidate']['url'].split('/')[-1][:-8],))
    elif change == 'download_unknown': db.execute("INSERT INTO thunder_queue(hash,state,committing) VALUES(?,'unknown',1)", (request['candidate']['url'].split('/')[-1][:-8],))
    elif change == 'dialog_unknown': db.set('thunder_dialog_hold_' + request['candidate']['url'].split('/')[-1][:-8], {'unconfirmed': True})
    elif change == 'sample':
        db.execute("INSERT INTO source_samples(candidate_id,show_id,family,state) VALUES(?,42,'fixture','preparing')", (cid,))
    elif change == 'blacklist': db.set('source_publisher_blacklist', [{'publisher': 'Group', 'aliases': ['Group'], 'reason': 'Synthetic exclusion'}])
    before = facts(db)
    with pytest.raises(ValueError): register_acquisition_declaration(db, path, apply=True)
    assert facts(db) == before


@pytest.mark.parametrize('change', ['report_episode', 'report_candidate', 'clean_claim', 'torrent_hash',
    'torrent_manifest', 'nfo_link', 'nfo_raw', 'nfo_filename', 'nfo_forced', 'nfo_no_tracks', 'outside_private'])
def test_actual_page_torrent_and_track_evidence_must_all_match(delivery, change):
    db, path, request, report = fixture(delivery)
    declaration = report['recommended_source_metadata_for_guarded_registration']
    if change == 'report_episode': report['episode'] = 3
    elif change == 'report_candidate': report['recommended_existing_candidate_id'] += 1
    elif change == 'clean_claim': report['clean_frames_verified'] = True
    elif change == 'torrent_hash': report['actual_torrent_manifest']['infohash'] = '0' * 40
    elif change == 'torrent_manifest': report['actual_torrent_manifest']['files'][0]['size'] += 1
    elif change == 'nfo_link': request['mediainfo_url'] = 'https://other.example.invalid/fake'
    elif change == 'outside_private': declaration['extracted_mediainfo'] = ref(db.path)
    elif change in ('nfo_raw', 'nfo_filename', 'nfo_forced', 'nfo_no_tracks'):
        p = Path(declaration['extracted_mediainfo']['path'])
        text = p.read_text(encoding='utf-8')
        if change == 'nfo_raw': text += 'Modified raw text without HTML evidence.'
        elif change == 'nfo_filename': text = text.replace('[Group] Example S01E02 [1080p].mkv', '[Group] Example S01E03 [1080p].mkv')
        elif change == 'nfo_forced': text = text.replace('Forced                                   : No', 'Forced                                   : Yes')
        else: text = text.split('Text #1')[0]
        p.write_text(text, encoding='utf-8')
        declaration['extracted_mediainfo'] = ref(p)
        if change != 'nfo_raw':
            p = Path(declaration['declaration_mediainfo']['path'])
            p.write_text('<pre data-media-info-text>' + escape(text) + '</pre>', encoding='utf-8')
            declaration['declaration_mediainfo'] = ref(p)
    update_report(path, request, report)
    before = facts(db)
    with pytest.raises(ValueError): register_acquisition_declaration(db, path, apply=True)
    assert facts(db) == before


def test_title_srtx2_without_a_real_track_declaration_remains_unknown(delivery):
    db, path, request, report = fixture(delivery)
    title = request['candidate']['title'] + ' SRTx2'
    db.execute('UPDATE candidates SET title=? WHERE id=?', (title, request['candidate_id']))
    request['candidate']['title'] = title
    declaration = report['recommended_source_metadata_for_guarded_registration']
    p = Path(declaration['extracted_mediainfo']['path'])
    text = 'General\nComplete name : External SRTx2.mp4\nFormat : MPEG-4\n'
    p.write_text(text, encoding='utf-8')
    declaration['extracted_mediainfo'] = ref(p)
    p = Path(declaration['declaration_mediainfo']['path'])
    p.write_text('<pre data-media-info-text>' + escape(text) + '</pre>', encoding='utf-8')
    declaration['declaration_mediainfo'] = ref(p)
    update_report(path, request, report)
    assert media_mode(db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))) == 'unknown'
    with pytest.raises(ValueError): register_acquisition_declaration(db, path, apply=True)


def test_transaction_rechecks_a_source_disabled_after_first_snapshot(delivery, monkeypatch):
    db, path, request, _ = fixture(delivery)
    connect, calls = db.connect, []
    @contextmanager
    def racing_connect():
        with connect() as con:
            yield con
        calls.append(True)
        if len(calls) == 1:
            with connect() as writer:
                writer.execute('UPDATE sources SET enabled=0 WHERE id=?', (request['source']['id'],))
    monkeypatch.setattr(db, 'connect', racing_connect)
    with pytest.raises(ValueError, match='disabled'):
        register_acquisition_declaration(db, path, apply=True)
    assert not db.one("SELECT 1 FROM sqlite_master WHERE name='subtitle_acquisition_declarations'")


def test_unsigned_metadata_marker_is_not_kept_by_rss_refresh(delivery):
    db, path, request, _ = fixture(delivery)
    register_acquisition_declaration(db, path, apply=True)
    db.execute('UPDATE candidates SET source_metadata=? WHERE id=?',
               (json.dumps({'media_mode': 'soft', 'acquisition_declaration': {'id': 'unregistered'}}), request['candidate_id']))
    source = db.one('SELECT * FROM sources WHERE id=?', (request['source']['id'],))
    Sources(db).remember(source, [request['candidate'] | {'source_metadata': {}}])
    assert media_mode(db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))) == 'unknown'
