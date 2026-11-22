"""Native title interpretation uses synthetic media/probes and an isolated DB."""
import json
import time

import pytest

from test_reliability import setup
from test_subtitle_studio import ready
from anime.subtitles import Subtitles
from anime.subtitle_chinese import (INSPECTION_VERSION, PIPELINE, _embedded_track_language,
    _inspection_path, chinese_preparation, collect_chinese_sources, completed_sources, digest)
from anime.subtitle_studio import archive_for


@pytest.mark.parametrize('title,expected', [
    ('CHS', 'chs'), ('cht', 'cht'), ('[Sakurato]CHS', 'chs'), ('[Sakurato]CHT', 'cht'),
    (' [Sakurato] CHS ', 'chs'), ('简体中文', 'chs'), ('繁體中文', 'cht'),
    ('Simplified Chinese', 'chs'), ('Traditional Chinese', 'cht'), ('中文', 'chi'),
])
@pytest.mark.parametrize('declared', ['', 'und'])
def test_explicit_single_language_title_can_supply_missing_language(title, expected, declared):
    track = {'language': declared or 'und', 'declared_language': declared, 'title': title}
    before = dict(track)
    effective, inference = _embedded_track_language(track)
    assert effective == expected and inference['method'] == 'explicit-und-track-title'
    assert inference['effective_language'] == expected and inference['version'] == 1
    assert track == before


@pytest.mark.parametrize('title', [
    '', 'Unknown', 'Default', 'CHS/CHT', 'CHS/JP', 'CHS + English', 'CHS-JPN',
    'CHS commentary', '简体中文/日文', 'Chinese and Japanese', '[JP]CHS', '[English]CHS',
    '[CHS CHT]CHS', '[双语]CHS', '[Sakurato]CHS/JP', '[Sakurato]CHS Extra',
])
def test_ambiguous_or_mixed_titles_and_display_labels_never_supply_language(title):
    assert _embedded_track_language({'language': 'und', 'title': title,
        'label': '简体中文'}) == ('und', None)


@pytest.mark.parametrize('declared', ['eng', 'jpn', 'chi', 'chs', 'mul'])
def test_declared_language_is_never_overridden_by_conflicting_title(declared):
    assert _embedded_track_language({'language': declared, 'declared_language': declared,
        'title': '[Sakurato]CHS'}) == (declared, None)


def test_sidecar_ownership_and_language_rules_do_not_gain_title_inference():
    assert _embedded_track_language({'origin': 'sidecar', 'language': 'und',
        'title': 'CHS'}) == ('und', None)


def test_probe_preserves_original_title_and_declared_language(setup, monkeypatch):
    db, *_ = setup
    media = db.path.parent / 'probe.mkv'
    media.write_bytes(b'synthetic media')
    streams = [
        {'index': 2, 'codec_name': 'ass', 'tags': {'title': '[Sakurato]CHS'}},
        {'index': 3, 'codec_name': 'ass', 'tags': {'title': 'CHT', 'language': 'eng'}},
    ]
    sub = Subtitles(db)
    monkeypatch.setattr(sub, 'tool', lambda name: name)
    monkeypatch.setattr(sub, 'run', lambda args, timeout: json.dumps({'streams': streams}).encode())
    tracks = {t['index']: t for t in sub.embedded_tracks({'path': str(media), 'size': media.stat().st_size})}
    assert tracks[2]['title'] == '[Sakurato]CHS' and tracks[2]['declared_language'] == ''
    assert tracks[2]['language'] == 'und' and tracks[2]['label'] == '简体中文'
    assert tracks[3]['title'] == 'CHT' and tracks[3]['declared_language'] == 'eng'
    assert tracks[3]['language'] == 'eng'


def native_fixture(setup, monkeypatch, titles=None, mixed=False):
    db, q, *_ = ready(setup)
    media = db.path.parent / 'Example.S01E02.mkv'
    media.write_bytes(b'synthetic registered media, not a real video')
    selected = {'index': 0, 'name': media.name, 'size': media.stat().st_size}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,purpose,acknowledged) "
        "VALUES(?,42,'[Group] Example S01E02','complete',?,?,'subtitle_sample',1)",
        ('a' * 40, json.dumps({'selected': {'2': selected}, 'files': [selected], 'ids': [0]}), str(media.parent)))
    titles = titles or ['[Sakurato]CHT', '[Sakurato]CHS', '[Sakurato]CHT', '[Sakurato]CHS']
    streams = [{'index': n + 2, 'codec_name': 'ass', 'tags': {'title': title}} for n, title in enumerate(titles)]
    calls = []

    def run(self, args, timeout):
        calls.append(args[0])
        if args[0] == 'ffprobe':
            return json.dumps({'streams': streams}).encode()
        index = int(args[args.index('-map') + 1].split(':')[1])
        text = '明天再見。' if index % 2 == 0 else '明天再见。'
        body = ('[Script Info]\nScriptType: v4.00+\n[Events]\n'
                'Format: Layer, Start, End, Style, Name, Text\n'
                f'Dialogue: 0,0:00:01.00,0:00:02.00,Text CN,,{text}\n')
        if mixed:
            body += 'Dialogue: 0,0:00:01.00,0:00:02.00,Text JP,,また明日。\n'
        return body.encode('utf-8')

    monkeypatch.setattr(Subtitles, 'tool', lambda self, name: name)
    monkeypatch.setattr(Subtitles, 'run', run)
    return db, q, media, streams, calls


def database_dump(db):
    with db.connect() as con:
        return list(con.iterdump())


@pytest.mark.requires_lab
def test_reinterpretation_archives_four_native_tracks_and_retains_old_inspection(setup, monkeypatch):
    db, q, media, streams, _ = native_fixture(setup, monkeypatch)
    item = completed_sources(db, 42, 2)[0]
    old_revision = digest(json.dumps([PIPELINE, item['fingerprint'], item['sidecars']], sort_keys=True).encode())
    new_path = _inspection_path(db, item)
    old_path = new_path.with_name(old_revision + '.json')
    old_path.parent.mkdir(parents=True)
    old_path.write_text(json.dumps({'pipeline': PIPELINE, 'fingerprint': item['fingerprint'],
        'state': 'no_chinese_text_track', 'draft_ids': []}), encoding='utf-8')
    old_bytes = old_path.read_bytes()
    # Keep a different production target and existing recovery facts unchanged.
    db.set('subtitle_delivery_readiness_42_2', {'target': {'fingerprint': 'b' * 64}})
    db.set('subtitle_preparation_history_fixture', {'attempts': 2, 'first_seen': 100, 'deadline_at': 200})
    before, media_bytes = database_dump(db), media.read_bytes()
    assert chinese_preparation(db, 42, 2)['action'] == 'collect_chinese_source'
    result = collect_chinese_sources(db, 42, 2)
    assert result['errors'] == [] and len(result['draft_ids']) == 4
    assert not result['production_ready_claimed'] and not result['target_changed'] and not result['owner_changed']
    archive = archive_for(db)
    for draft_id in result['draft_ids']:
        draft = archive.draft(draft_id)
        meta = draft['metadata']
        assert draft['language'] in ('chs', 'cht')
        assert meta['declared_language'] == ''
        assert meta['track_title'] == streams[meta['track'] - 2]['tags']['title']
        assert meta['language_inference']['method'] == 'explicit-und-track-title'
        assert meta['media_fingerprint'] == item['fingerprint']
        assert meta.get('target_matching_status') != 'verified'
        assert not meta['mixed_language_suspected']
    record = json.loads(new_path.read_bytes())
    assert record['inspection_version'] == INSPECTION_VERSION and record['state'] == 'chinese_text_archived'
    assert all(t['language'] == 'und' and t['effective_language'] in ('chs', 'cht') for t in record['tracks'])
    current = completed_sources(db, 42, 2)[0]
    assert current['inspection_path'] == str(new_path) and current['state'] == 'chinese_text_archived'
    assert current['fingerprint'] == item['fingerprint']
    new_bytes = new_path.read_bytes()
    repeated = collect_chinese_sources(db, 42, 2)
    assert repeated['draft_ids'] == result['draft_ids'] and repeated['errors'] == []
    assert old_path.read_bytes() == old_bytes and new_path.read_bytes() == new_bytes
    assert database_dump(db) == before and media.read_bytes() == media_bytes and q.adds == 0


@pytest.mark.requires_lab
def test_inferred_chinese_still_retains_and_separates_mixed_original(setup, monkeypatch):
    db, q, *_ = native_fixture(setup, monkeypatch, titles=['[Sakurato]CHS'], mixed=True)
    result = collect_chinese_sources(db, 42, 2)
    assert not result['errors'] and len(result['draft_ids']) == 3
    drafts = [archive_for(db).draft(i) for i in result['draft_ids']]
    raw = next(d for d in drafts if d['metadata']['origin'] == 'embedded')
    assert raw['language'] == 'chs' and raw['metadata']['mixed_language_suspected']
    split = {d['language']: d for d in drafts if d['metadata']['origin'] == 'explicit-ass-language-style'}
    assert set(split) == {'chi', 'jpn'}
    assert split['chi']['cues'][0]['text'] == '明天再見。'
    assert split['jpn']['cues'][0]['text'] == 'また明日。'
    assert not split['chi']['metadata']['mixed_language_suspected']
    assert split['chi']['metadata']['target_matching_status'] == 'pending'
    assert split['chi']['metadata']['source_draft_id'] == raw['id']
    assert not result['production_ready_claimed'] and q.adds == 0


@pytest.mark.requires_lab
@pytest.mark.parametrize('protection', ['watched', 'owner', 'unknown', 'lease', 'scope'])
def test_inferred_title_never_bypasses_preparation_protection(setup, monkeypatch, protection):
    db, q, _, _, calls = native_fixture(setup, monkeypatch)
    if protection == 'watched':
        db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    elif protection == 'scope':
        db.execute('UPDATE shows SET selected=0 WHERE id=42')
    else:
        db.execute("INSERT INTO subtitle_delivery_jobs(id,show_id,episode,kind,state,input_key,payload,created,updated,"
            "thread_id,dispatch_state,lease_owner,lease_expires) VALUES('fixture',42,2,'episode','blocked','input','{}',1,1,?,?,?,?)",
            ('original-owner' if protection == 'owner' else '', 'unknown' if protection == 'unknown' else '',
             'other-lease' if protection == 'lease' else '', time.time() + 60 if protection == 'lease' else 0))
    before = database_dump(db)
    with pytest.raises(ValueError):
        collect_chinese_sources(db, 42, 2)
    assert not calls and database_dump(db) == before and q.adds == 0


@pytest.mark.requires_lab
def test_inferred_track_rechecks_completed_task_after_extraction(setup, monkeypatch):
    db, q, _, _, _ = native_fixture(setup, monkeypatch, titles=['[Sakurato]CHS'])
    original_run = Subtitles.run

    def run(self, args, timeout):
        raw = original_run(self, args, timeout)
        if args[0] == 'ffmpeg':
            db.execute("UPDATE tasks SET status='uncertain'")
        return raw

    monkeypatch.setattr(Subtitles, 'run', run)
    result = collect_chinese_sources(db, 42, 2)
    assert result['draft_ids'] == [] and len(result['errors']) == 1
    assert 'ownership, receipt or sidecar changed' in result['errors'][0]['error']
    assert not archive_for(db).bases(42, 1, 2) and q.adds == 0
