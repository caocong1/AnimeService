import json
from types import SimpleNamespace
from unittest.mock import Mock

from anime.db import Store
from anime.subtitle_studio import authored


def test_fidelity_checks_each_distinct_draft_once_per_request_and_still_rejects_a_late_blocker(tmp_path, monkeypatch):
    db = Store(tmp_path / 'test.db')
    db.upsert_subject({'id': 42, 'name': 'Example'})
    db.set('subtitle_studio_enabled', True)
    archive = Mock(root=tmp_path)
    selected = {'id': 'test-build'}
    archive.selected.return_value = selected
    archive.draft.side_effect = lambda key: {'metadata': {'release': key}}
    monkeypatch.setattr('anime.subtitle_studio.archive_for', lambda db: archive)
    monkeypatch.setattr('anime.subtitle_studio.lab_core', lambda: SimpleNamespace(fingerprint=lambda *args: 'fp'))
    monkeypatch.setattr('anime.subtitle_targets.default_binding', lambda *args: (False, None))
    blocker = Mock(side_effect=lambda db, release: release == 'blocked')
    monkeypatch.setattr('anime.subtitle_studio.fidelity_blocker', blocker)
    book = tmp_path / 'builds/test-build/decisions.json'
    book.parent.mkdir(parents=True)
    cues = [{'source': {'draft_id': 'zh'}, 'japanese_source': {'draft_id': 'ja'}} for _ in range(1000)]
    book.write_text(json.dumps({'cues': cues}), encoding='utf-8')
    item = {'show_id': 42, 'episode': 1, 'path': 'test.mkv', 'size': 10}
    assert authored(db, item) == selected
    assert archive.draft.call_count == blocker.call_count == 2
    assert {call.args[0] for call in archive.draft.call_args_list} == {'zh', 'ja'}
    # New source near the end is still checked; prior calls grant no exemption.
    cues.append({'source': {'draft_id': 'blocked'}, 'japanese_source': {}})
    book.write_text(json.dumps({'cues': cues}), encoding='utf-8')
    archive.draft.reset_mock()
    assert authored(db, item) is None
    assert archive.draft.call_count == 3
    blocker.side_effect = lambda db, release: release == 'zh'
    assert authored(db, item) is None
