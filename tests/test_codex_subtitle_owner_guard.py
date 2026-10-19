import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import codex_subtitle_owner_guard as guard


def test_no_progress_is_separate_from_elapsed_production_time():
    turn = {'status': 'inProgress', 'started_at': 1}
    decide = lambda **kw: guard.decide({}, kw.get('cwd', 'D:/AnimeService'),
        kw.get('turn', turn), kw.get('items', []), kw.get('log', 50), 1300, 1200, 'D:/AnimeService')
    assert decide() == 'interrupt_stale_owner_wait'
    assert decide(log=1290) == 'recent_progress'
    assert decide(items=[{'type': 'commandExecution', 'status': 'inProgress'}]) == 'tool_in_progress'
    assert decide(items=[{'type': 'mcpToolCall', 'status': 'running'}]) == 'tool_in_progress'
    assert decide(items=[{'type': 'futureExternalTool', 'status': 'pending'}]) == 'tool_in_progress'
    assert decide(items=[{'type': 'contextCompaction', 'status': 'inProgress'}]) == 'interrupt_stale_owner_wait'
    assert decide(turn={'status': 'interrupted'}) == 'terminal_or_idle'
    assert decide(cwd='E:/Other') == 'outside_scope'


def test_only_current_acknowledged_unwatched_authorized_owner(tmp_path):
    path = tmp_path / 'anime.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE shows(id,selected,state,authorized,cleanup_hold);
            CREATE TABLE watches(show_id,episode,finished);
            CREATE TABLE subtitle_delivery_jobs(id,show_id,episode,state,dispatch_state,thread_id,input_key,revision,payload);
            INSERT INTO shows VALUES(1,1,'watching',1,0);
            INSERT INTO subtitle_delivery_jobs VALUES('job',1,3,'production','acknowledged','owner','input',7,'{}');''')
    assert guard.owners(path)[0]['thread_id'] == 'owner'
    for column, value in [('dispatch_state', 'unknown'), ('dispatch_state', 'committing'), ('dispatch_state', 'blocked-stopped'), ('state', 'finished'), ('payload', '{"owner_stopped_writing":true}')]:
        with sqlite3.connect(path) as db:
            db.execute(f'UPDATE subtitle_delivery_jobs SET {column}=?', (value,))
        assert guard.owners(path) == []
        with sqlite3.connect(path) as db:
            db.execute("UPDATE subtitle_delivery_jobs SET state='production',dispatch_state='acknowledged',payload='{}'")
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO watches VALUES(1,3,1)')
    assert guard.owners(path) == []


def test_control_records_intent_and_never_repeats_unknown_result(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, 'ROOT', tmp_path)
    row = {'id': 'job', 'thread_id': 'owner', 'input_key': 'input', 'revision': 1,
           'turn_id': 'turn', 'decision': 'interrupt_stale_owner_wait'}
    monkeypatch.setattr(guard, 'snapshot', lambda *args: [dict(row)])
    monkeypatch.setattr(guard, 'node_path', lambda *args: 'node')
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        # Intent must already exist before any host operation.
        assert (tmp_path / 'data/diagnostics/codex-subtitle-owner-guard/actions.jsonl').exists()
        raise TimeoutError('unknown native outcome')
    monkeypatch.setattr(guard.subprocess, 'run', fail)
    config = {'owner_db': str(tmp_path/'db'), 'cwd': 'D:/AnimeService'}
    assert guard.run(True, tmp_path, config, now=1300)['error']
    status = guard.run(True, tmp_path, config, now=1301)
    assert status['threads'][0]['decision'] == 'already_attempted'
    assert len(calls) == 1


def test_changed_owner_or_turn_before_control_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, 'ROOT', tmp_path)
    first = {'id': 'job', 'thread_id': 'owner', 'input_key': 'input', 'revision': 1,
             'turn_id': 'old', 'decision': 'interrupt_stale_owner_wait'}
    snapshots = iter([[dict(first)], [{**first, 'turn_id': 'new'}]])
    monkeypatch.setattr(guard, 'snapshot', lambda *args: next(snapshots))
    status = guard.run(True, tmp_path, {}, now=1300)
    assert status['threads'][0]['decision'] == 'changed_before_control'
    assert not (tmp_path/'data/diagnostics/codex-subtitle-owner-guard/actions.jsonl').exists()


def test_exclusive_intent_prevents_racing_or_unknown_control(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setattr(guard, 'ROOT', tmp_path)
    row = {'id': 'job', 'thread_id': 'owner', 'input_key': 'input', 'revision': 1,
           'turn_id': 'turn', 'decision': 'interrupt_stale_owner_wait'}
    monkeypatch.setattr(guard, 'snapshot', lambda *args: [dict(row)])
    run_dir = tmp_path/'data/diagnostics/codex-subtitle-owner-guard'
    run_dir.mkdir(parents=True)
    key = hashlib.sha256(b'owner:turn').hexdigest()
    (run_dir/('intent-'+key+'.json')).write_text('{unknown outcome}')
    def forbidden(*args, **kwargs):
        raise AssertionError('must not repeat native operation')
    monkeypatch.setattr(guard.subprocess, 'run', forbidden)
    result = guard.run(True, tmp_path, {}, now=1300)
    assert result['threads'][0]['decision'] == 'already_attempted'
    assert not (run_dir/'actions.jsonl').exists()
