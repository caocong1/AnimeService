"""Deterministic archive rejections are recorded once per input; derived tracks stay immutable.

The private subtitle core is replaced by a synthetic core and archive, so these run without the lab.
"""
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_reliability import setup
import anime.subtitle_studio as studio_module
from anime.subtitle_chinese import archive_text_source


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fake_core(time_format='{:d}'):
    cues = [{'id': 'c1', 'style': 'Text CN', 'text': '明天再见。', 'start_ms': 1000, 'end_ms': 2000},
            {'id': 'c2', 'style': 'Text JP', 'text': 'また明日。', 'start_ms': 1000, 'end_ms': 2000}]
    return SimpleNamespace(
        ZH={'chi', 'zho'}, JA={'jpn'}, digest=digest,
        canonical=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False),
        fingerprint=lambda path, size=None: 'fp-' + Path(path).name,
        parse_subtitles=lambda raw, ext: [dict(c) for c in cues],
        vtt_time=lambda ms: time_format.format(ms))


class FakeArchive:
    """add_base keeps the core's identity rule: same identity, language and bytes is the same draft."""
    def __init__(self, root):
        self.root = Path(root);self.drafts = {};self.calls = []

    def register(self, *args, **kwargs):
        return 'key'

    def add_base(self, sid, season, ep, path, metadata):
        data = Path(path).read_bytes();self.calls.append(Path(path).name)
        if b'BROKEN' in data:
            raise ValueError('empty subtitles or invalid timing')
        identity = [ep] + [metadata.get(k) for k in ('source_key', 'track', 'media_fingerprint', 'language')] + [digest(data)]
        draft_id = digest(json.dumps(identity).encode())[:12]
        self.drafts.setdefault(draft_id, {'id': draft_id, 'language': metadata.get('language'),
                                          'metadata': {**metadata, 'raw_hash': digest(data)}})
        return draft_id

    def bases(self, sid, season, ep):
        return list(self.drafts.values())


ASS = ('[Script Info]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Text CN,,明天再见。\n'
       'Dialogue: 0,0:00:01.00,0:00:02.00,Text JP,,また明日。\n')
META = {'source_key': 'h', 'track': 100000, 'media_fingerprint': 'fp-video', 'language': 'chi'}


def test_changed_parser_keeps_the_registered_derivation(tmp_path):
    source = tmp_path / 'source.ass';source.write_text(ASS, encoding='utf-8')
    archive = FakeArchive(tmp_path / 'archive')
    first = archive_text_source(archive, fake_core(), 42, 1, 1, source, META)
    assert len(first) == 3 and len(archive.drafts) == 3
    staged = archive.root / 'derived-language-tracks' / digest(source.read_bytes())
    before = {p.name: p.read_bytes() for p in staged.iterdir()}
    assert set(before) == {'chi.vtt', 'jpn.vtt'}
    again = archive_text_source(archive, fake_core('{:08d}'), 42, 1, 1, source, META)
    assert again == first and len(archive.drafts) == 3
    assert {p.name: p.read_bytes() for p in staged.iterdir()} == before


def test_unregistered_staging_file_is_kept_and_the_new_body_is_staged_beside_it(tmp_path):
    source = tmp_path / 'source.ass';source.write_text(ASS, encoding='utf-8')
    archive = FakeArchive(tmp_path / 'archive')
    staged = archive.root / 'derived-language-tracks' / digest(source.read_bytes())
    staged.mkdir(parents=True);(staged / 'chi.vtt').write_bytes(b'WEBVTT\n\n100')  # torn, never registered
    ids = archive_text_source(archive, fake_core(), 42, 1, 1, source, META)
    assert len(ids) == 3 and (staged / 'chi.vtt').read_bytes() == b'WEBVTT\n\n100'
    sibling = [p for p in staged.iterdir() if p.name.startswith('chi-')]
    assert len(sibling) == 1 and b'1000 --> 2000' in sibling[0].read_bytes()
    assert archive_text_source(archive, fake_core(), 42, 1, 1, source, META) == ids and len(archive.drafts) == 3
    assert not [p for p in staged.iterdir() if p.name.endswith('.tmp')]


class SyncThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def studio(setup, monkeypatch, tmp_path):
    """A completed task whose bad Chinese sidecar is listed before a good Japanese one."""
    db, q, e, c = setup
    db.set('subtitle_studio_enabled', True)
    root = tmp_path / 'library' / 'Example';root.mkdir(parents=True)
    video = root / 'Example - 01.mkv';video.write_bytes(b'v' * 100)
    bad = root / 'Example - 01.chs.srt';bad.write_bytes(b'1\n00:00:01,000 --> 00:00:01,000\nBROKEN\n')
    good = root / 'Example - 01.jpn.srt';good.write_bytes(b'1\n00:00:01,000 --> 00:00:02,000\nmata\n')
    files = [{'index': i, 'name': p.name, 'size': p.stat().st_size} for i, p in enumerate((bad, good, video))]
    selection = {'files': files, 'ids': [0, 1, 2], 'selected': {'1': files[2]}}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,created,updated) VALUES('h',42,'[Group] Example - 01','complete',?,?,0,0)",
               (json.dumps(selection), str(root)))
    archive = FakeArchive(tmp_path / 'archive');now = [1000.0]
    s = e.subtitle_studio
    monkeypatch.setattr(studio_module, 'lab_core', lambda: fake_core())
    monkeypatch.setattr(studio_module, 'archive_for', lambda db: archive)
    monkeypatch.setattr(studio_module, 'threading', SimpleNamespace(Thread=SyncThread))
    monkeypatch.setattr(studio_module, 'time', SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(s, 'ensure', lambda sid, ep: None)
    monkeypatch.setattr(s, 'refresh_job', lambda *args, **kwargs: None)
    monkeypatch.setattr(s.sub, 'tracks', lambda item: [])
    item = {'show_id': 42, 'episode': 1, 'path': str(video), 'size': 100, 'hash': 'h',
            'title': '[Group] Example - 01', 'fingerprint': 'fp-' + video.name, 'inventory_revision': 'r1'}
    items = [item]
    monkeypatch.setattr(s, 'media', lambda: [dict(i) for i in items])
    return db, s, archive, items, bad, now


def warnings(db):
    return [r['message'] for r in db.rows("SELECT message FROM events WHERE scope='subtitle-studio' ORDER BY id")]


def test_rejected_sidecar_is_recorded_once_and_retried_only_when_it_changes(studio):
    db, s, archive, items, bad, now = studio
    for _ in range(3):
        s.tick();now[0] += 600
    assert archive.calls == ['Example - 01.chs.srt', 'Example - 01.jpn.srt']  # the later good sidecar is archived
    payload = json.loads(db.one('SELECT payload FROM subtitle_studio_media')['payload'])
    assert payload['inventory_revision'] == 'r1' and len(payload['draft_ids']) == 1
    assert payload['errors'] == ['sidecar 0 ValueError: empty subtitles or invalid timing']
    assert warnings(db) == ['42 E1 字幕归档跳过 1 项输入，详见归档清单']
    bad.write_bytes(bad.read_bytes().replace(b'BROKEN', b'fixed!'))  # same size, new sha -> new revision
    items[0]['inventory_revision'] = 'r2'
    s.tick();s.tick()
    assert archive.calls[2:] == ['Example - 01.chs.srt', 'Example - 01.jpn.srt']
    assert json.loads(db.one('SELECT payload FROM subtitle_studio_media')['payload'])['errors'] == []
    assert len(warnings(db)) == 1


def test_failing_media_backs_off_without_starving_later_media(studio, monkeypatch):
    db, s, archive, items, bad, now = studio
    videos = [Path(items[0]['path']).with_name(f'Example - 0{ep}.mkv') for ep in (1, 2, 3)]
    for video in videos[1:]:
        video.write_bytes(b'v' * 100)
    items[:] = [{**items[0], 'path': str(video), 'fingerprint': fp, 'episode': ep}
                for ep, fp, video in zip((1, 2, 3), ('f1', 'f2', 'f3'), videos)]
    monkeypatch.setattr(studio_module, 'lab_core', lambda: SimpleNamespace(
        **{**vars(fake_core()), 'fingerprint': lambda path, size=None: 'f' + Path(path).stem[-1]}))
    attempts = [];collect = s.collect
    def flaky(item):
        attempts.append(item['fingerprint'])
        if item['fingerprint'] != 'f3':
            raise ValueError('immutable base changed')
        return collect(item)
    monkeypatch.setattr(s, 'collect', flaky)
    s.tick();s.tick()
    assert attempts == ['f1', 'f2', 'f3']  # failures wait out their backoff without taking slots
    assert db.one("SELECT 1 FROM subtitle_studio_media WHERE fingerprint='f3'")
    events = warnings(db)
    assert len(events) == 2 and all('网络' not in m for m in events)
    assert events[0].startswith('42 E1 ValueError：字幕归档拒绝（subtitle_studio.')
    assert events[1].startswith('42 E2 ValueError：字幕归档拒绝（subtitle_studio.')
    failure = db.get('subtitle_studio_collect_error_f1')
    assert failure['count'] == 1 and failure['revision'] == 'r1' and failure['next'] == 1300
    for _ in range(4):
        now[0] = db.get('subtitle_studio_collect_error_f1')['next'];s.tick()
    assert db.get('subtitle_studio_collect_error_f1')['count'] == 5
    assert [m for m in warnings(db) if m.startswith('42 E1')] == [events[0], events[0]]  # marks 1 and 5 only
    monkeypatch.setattr(s, 'collect', collect)
    now[0] = db.get('subtitle_studio_collect_error_f1')['next'];s.tick()
    assert db.get('subtitle_studio_collect_error_f1') is None
    assert db.one("SELECT 1 FROM subtitle_studio_media WHERE fingerprint='f1'")
