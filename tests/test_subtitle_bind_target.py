"""A provisional target is only ever a reconciled completed file of this episode; binding it never claims readiness."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import anime.subtitle_studio as studio
from anime.subtitle_evidence import bind_target, register_evidence, current_target
from test_subtitle_delivery import delivery  # noqa: F401  (fixture)
from test_subtitle_evidence import archive, write, capability

spec = importlib.util.spec_from_file_location('subtitle_delivery_queue', Path(__file__).resolve().parents[1] / 'scripts/subtitle_delivery_queue.py')
queue_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_cli)


@pytest.fixture
def completed(delivery, monkeypatch):
    """One reconciled completed task whose selected file for 42/1 exists on disk; the lab fingerprint is synthetic."""
    db, scheduler, ready, now = delivery
    library = db.path.parent.parent / 'library' / '42'
    library.mkdir(parents=True)
    media = library / 'Example S01E01.mkv'
    media.write_bytes(b'synthetic media bytes')
    selection = {'ids': [0], 'files': [{'index': 0, 'name': media.name}], 'selected': {'1': {'name': media.name, 'size': media.stat().st_size}}}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,purpose) VALUES('task-42-1',42,'[Group] Example S01E01','complete',?,?,'episode')",
               (json.dumps(selection), str(library)))
    monkeypatch.setattr(studio, 'lab_core', lambda: SimpleNamespace(fingerprint=lambda p: 'fp-' + Path(p).name))
    return db, scheduler, media


def test_bind_records_the_file_and_later_evidence_binds_to_it(completed):
    db, scheduler, media = completed
    preview = bind_target(scheduler, 42, 1, media, dry_run=True)
    assert preview['would_bind'] and preview['fingerprint'] == 'fp-Example S01E01.mkv' and not preview['replay']
    assert 'target' not in (db.get('subtitle_delivery_readiness_42_1') or {})
    result = bind_target(scheduler, 42, 1, media)
    assert result['bound'] and not result['replayed'] and result['state'] == 'ready'
    target = db.get('subtitle_delivery_readiness_42_1')['target']
    assert target['path'] == str(media.resolve()) and target['size'] == media.stat().st_size
    assert target['fingerprint'] == 'fp-Example S01E01.mkv' and target['task_hash'] == 'task-42-1'
    assert current_target(db, 42, 1) == target
    events = [json.loads(r['payload']) for r in db.rows("SELECT payload FROM subtitle_delivery_events WHERE job_id='episode:42:1' AND kind='target-bound'")]
    assert events == [{'fingerprint': 'fp-Example S01E01.mkv', 'size': media.stat().st_size, 'task_hash': 'task-42-1'}]
    again = bind_target(scheduler, 42, 1, media)
    assert again['replayed'] and len(db.rows("SELECT 1 FROM subtitle_delivery_events WHERE kind='target-bound'")) == 1
    root = archive(db)
    stale = write(root, 'stale.json', capability())  # still names the studio job's old media
    with pytest.raises(ValueError, match='different media'):
        register_evidence(scheduler, 42, 1, 'delivery_capability_report', stale)
    fresh = write(root, 'fresh.json', capability(target_fingerprint='fp-Example S01E01.mkv'))
    assert register_evidence(scheduler, 42, 1, 'delivery_capability_report', fresh)['registered']


def test_only_the_reconciled_selection_of_this_episode_can_be_bound(completed):
    db, scheduler, media = completed
    other = media.with_name('Example S01E02.mkv')
    other.write_bytes(b'other')
    with pytest.raises(ValueError, match='reconciled task'):
        bind_target(scheduler, 42, 1, other)
    with pytest.raises(ValueError, match='existing file'):
        bind_target(scheduler, 42, 1, media.with_name('missing.mkv'))
    media.write_bytes(b'grown after reconciliation')
    with pytest.raises(ValueError, match='size differs'):
        bind_target(scheduler, 42, 1, media)
    media.write_bytes(b'synthetic media bytes')
    db.execute("UPDATE tasks SET status='downloading' WHERE hash='task-42-1'")
    with pytest.raises(ValueError, match='reconciled task'):
        bind_target(scheduler, 42, 1, media)
    db.execute("UPDATE tasks SET status='complete',purpose='research' WHERE hash='task-42-1'")
    with pytest.raises(ValueError, match='reconciled task'):
        bind_target(scheduler, 42, 1, media)
    assert 'target' not in (db.get('subtitle_delivery_readiness_42_1') or {})


def test_frozen_or_protected_episodes_refuse_binding(completed):
    db, scheduler, media = completed
    db.execute('CREATE TABLE subtitle_targets(id TEXT PRIMARY KEY,show_id INTEGER,episode INTEGER,season INTEGER,payload TEXT NOT NULL,created REAL)')
    db.execute('CREATE TABLE subtitle_target_heads(show_id INTEGER,episode INTEGER,target_id TEXT NOT NULL,PRIMARY KEY(show_id,episode))')
    db.execute("INSERT INTO subtitle_targets VALUES('t1',42,1,1,?,0)", (json.dumps({'target': {'fingerprint': 'frozen'}}),))
    db.execute("INSERT INTO subtitle_target_heads VALUES(42,1,'t1')")
    with pytest.raises(ValueError, match='frozen'):
        bind_target(scheduler, 42, 1, media)
    db.execute('DELETE FROM subtitle_target_heads')
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    with pytest.raises(ValueError, match='Watched'):
        bind_target(scheduler, 42, 1, media)


def test_cli_binds_and_dry_runs_read_only(completed, monkeypatch, capsys):
    db, scheduler, media = completed
    monkeypatch.setattr(queue_cli, 'ControlStore', lambda path: db)
    monkeypatch.setattr(queue_cli, 'SubtitleDelivery', lambda store, initialize=True: scheduler)
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), 'bind-target', '42', '1', '--path', str(media), '--dry-run'])
    queue_cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed['would_bind'] and 'target' not in (db.get('subtitle_delivery_readiness_42_1') or {})
    monkeypatch.setattr(sys, 'argv', ['queue', '--db', str(db.path), 'bind-target', '42', '1', '--path', str(media)])
    queue_cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed['bound'] and db.get('subtitle_delivery_readiness_42_1')['target']['fingerprint'] == 'fp-Example S01E01.mkv'
    assert 'lease_token' not in json.dumps(printed) and 'dispatch_token' not in json.dumps(printed)
