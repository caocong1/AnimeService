import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient

from anime.app import create_app
from anime.db import Store
from anime.read_projection import read_projection
from anime.subtitle_chinese import archived_chinese_sources, _small_cue_count


def test_snapshot_reuses_lookups_without_sharing_mutations_or_stale_requests(tmp_path):
    db = Store(tmp_path / 'test.db')
    db.upsert_subject({'id': 42, 'name': 'Example'})
    db.set('value', {'items': [1]})
    with read_projection(db.path) as view:
        selects = []
        view.con.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith('SELECT') else None)
        for _ in range(100):
            view.show(42)['title'] = 'local annotation'
            view.get('value')['items'].append(2)
            assert view.show(42)['title'] == 'Example'
            assert view.get('value') == {'items': [1]}
        assert len(selects) == 2
        assert view.get('absent', 1) == 1 and view.get('absent', 2) == 2
        db.set('value', {'items': [3]})
        assert view.get('value') == {'items': [1]}  # the same read transaction
    with read_projection(db.path) as view:
        assert view.get('value') == {'items': [3]}


def test_status_uses_one_projection_even_with_many_review_tasks(tmp_path, monkeypatch):
    db = Store(tmp_path / 'test.db')
    app = create_app(db, Mock(), start_worker=False)
    with db.connect() as con:
        con.executemany("INSERT INTO tasks(hash,title,status) VALUES(?,?,'review')",
                        [(str(i), 'Example') for i in range(100)])
    import anime.read_projection as module
    original = module.ProjectionStore.__init__
    snapshots = []
    def opened(self, path):
        snapshots.append(path)
        original(self, path)
    monkeypatch.setattr(module.ProjectionStore, '__init__', opened)
    result = TestClient(app, base_url='http://localhost:4871').get('/api/status')
    assert result.status_code == 200 and len(result.json()['task_reviews']) == 100
    assert len(snapshots) == 1


def test_cue_count_reuse_cannot_bypass_changed_bytes_or_source_exclusion(tmp_path, monkeypatch):
    import hashlib
    db = Store(tmp_path / 'test.db')
    db.upsert_subject({'id': 42, 'name': 'Example'})
    root = tmp_path / 'subtitle-studio'
    root.mkdir()
    source = root / 'source.ass'
    source.write_bytes(b'first bytes')
    raw_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    parser = Mock(return_value=[{}, {}])
    core = SimpleNamespace(ZH={'chi'}, episode_key=lambda *args: 'episode', parse_subtitles=parser)
    monkeypatch.setattr('anime.subtitle_lab.lab_core', lambda: core)
    with sqlite3.connect(root / 'archive.sqlite3') as con:
        con.execute('CREATE TABLE drafts(id,raw_hash,text_hash,language,metadata,episode_key)')
        con.execute('INSERT INTO drafts VALUES(?,?,?,?,?,?)',
                    ('draft', raw_hash, 'text', 'chi', json.dumps({'raw_file': str(source), 'release': '[Group] Example'}), 'episode'))
    _small_cue_count.cache_clear()
    try:
        assert archived_chinese_sources(db, 42, 1)[0]['cue_count'] == 2
        assert archived_chinese_sources(db, 42, 1)[0]['cue_count'] == 2
        assert parser.call_count == 1
        source.write_bytes(b'changed bytes')
        assert archived_chinese_sources(db, 42, 1) == []
        assert parser.call_count == 1
        with sqlite3.connect(root / 'archive.sqlite3') as con:
            con.execute('UPDATE drafts SET raw_hash=?', (hashlib.sha256(source.read_bytes()).hexdigest(),))
        assert archived_chinese_sources(db, 42, 1)[0]['cue_count'] == 2
        assert parser.call_count == 2
        monkeypatch.setattr('anime.subtitle_chinese.fidelity_blocker', lambda *args: 'excluded now')
        assert archived_chinese_sources(db, 42, 1) == []
    finally:
        _small_cue_count.cache_clear()


def test_source_decision_rechecks_current_rules_in_a_single_read_snapshot(tmp_path, monkeypatch):
    from anime.source_review import SourceReview
    import anime.read_projection as module
    db = Store(tmp_path / 'test.db')
    db.upsert_subject({'id': 42, 'name': 'Example'})
    db.execute('INSERT INTO source_policies VALUES(?,?,0)', (42, json.dumps({'mode': 'subtitle_only'})))
    reader = SourceReview.__new__(SourceReview)
    reader.db = db
    original = module.ProjectionStore.__init__
    opened = []
    def snapshot(self, path):
        opened.append(path)
        original(self, path)
    monkeypatch.setattr(module.ProjectionStore, '__init__', snapshot)
    candidate = {'id': 1, 'show_id': 42, 'title': '[Group] Example - 01 [1080p]'}
    assert reader.decision(candidate)['allowed'] is True
    db.set('source_publisher_blacklist', [{'publisher': 'Group', 'aliases': ['Group'], 'reason': 'fixture'}])
    assert reader.decision(candidate)['allowed'] is False
    assert len(opened) == 2
