import pytest
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('guard', Path(__file__).resolve().parents[1] / 'scripts/codex_dispatch_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
TID = next(iter(guard.TARGETS))
TURN = {'status': 'inProgress', 'started_at': 1}
USER = {'type': 'userMessage', 'content': [{'type': 'text', 'text': '<heartbeat><automation_id>automation-3</automation_id></heartbeat>'}]}


def test_only_stale_scheduled_model_wait_is_interrupted():
    assert guard.decide(TID, 'D:\\AnimeService', TURN, [USER], 50, 1300) == 'interrupt_stale_model_wait'
    assert guard.decide(TID, '\\\\?\\D:\\AnimeService', TURN, [USER], 50, 1300) == 'interrupt_stale_model_wait'
    assert guard.decide(TID, 'D:/AnimeService', TURN, [USER], 500, 1300) == 'recent_progress'


def test_manual_turns_and_compaction_are_excluded():
    assert guard.decide(TID, 'D:/AnimeService', TURN, [], 50, 1300) == 'not_scheduled_turn'
    assert guard.decide(TID, 'D:/AnimeService', TURN, [{'type': 'userMessage', 'content': [{'type': 'text', 'text': 'manual'}]}], 50, 1300) == 'not_scheduled_turn'


def test_active_tools_are_never_interrupted():
    assert guard.decide(TID, 'D:/AnimeService', TURN, [USER, {'type': 'commandExecution', 'status': 'inProgress'}], 50, 1300) == 'tool_in_progress'


def test_other_threads_projects_and_completed_turns_are_excluded():
    assert guard.decide('episode-owner', 'D:/AnimeService', TURN, [USER], 50, 1300) == 'outside_scope'
    assert guard.decide(TID, 'D:/MediaService', TURN, [USER], 50, 1300) == 'outside_scope'
    assert guard.decide(TID, 'D:/AnimeService', {'status': 'completed'}, [USER], 50, 1300) == 'idle'
    assert guard.decide(TID, 'D:/AnimeService', None, [], 50, 1300) == 'idle'


def test_another_automation_cannot_authorize_interrupt():
    assert guard.decide(TID, 'D:/AnimeService', TURN, [{'type': 'userMessage', 'content': [{'type': 'text', 'text': '<heartbeat><automation_id>automation-2</automation_id></heartbeat>'}]}], 50, 1300) == 'not_scheduled_turn'


def test_only_exact_registered_recovery_is_guarded():
    user = {'type': 'functionCallOutput', 'name': 'send_message_to_thread', 'output': '<codex_delegation>recovery</codex_delegation>'}
    turn = {**TURN, 'turn_id': guard.RECOVERY_TURNS[TID]}
    assert guard.decide(TID, 'D:/AnimeService', turn, [user], 50, 1300) == 'interrupt_stale_model_wait'
    assert guard.decide(TID, 'D:/AnimeService', {**turn, 'turn_id': 'another-user-turn'}, [user], 50, 1300) == 'not_scheduled_turn'


def test_config_file_defines_scope_and_timing(tmp_path):
    import json
    cfg_path = tmp_path / 'dispatch.json'
    cfg_path.write_text(json.dumps({'cwd': 'E:\\Other', 'targets': {'t-1': 'auto-x'}, 'recovery_turns': {}, 'wait_seconds': 60}), encoding='utf-8')
    cfg = guard.load_config(cfg_path)
    user = {'type': 'userMessage', 'content': [{'type': 'text', 'text': '<heartbeat><automation_id>auto-x</automation_id></heartbeat>'}]}
    assert guard.decide('t-1', 'e:/other/', TURN, [user], 50, 130, config=cfg) == 'interrupt_stale_model_wait'
    assert guard.decide('t-1', 'e:/other', TURN, [user], 100, 130, config=cfg) == 'recent_progress'  # 60 s threshold
    assert guard.decide('t-1', 'D:/AnimeService', TURN, [user], 50, 130, config=cfg) == 'outside_scope'
    assert guard.decide(TID, 'D:/AnimeService', TURN, [USER], 50, 1300, config=cfg) == 'outside_scope'  # the default scope is not implied
    assert guard.decide(TID, 'D:/AnimeService', TURN, [USER], 50, 1300) == 'interrupt_stale_model_wait'  # module default unaffected
    for bad in [5, 'abc', True, 10 ** 6]:  # seconds; outside 60..86400 or not an integer falls back to the old 20 minutes
        cfg_path.write_text(json.dumps({'cwd': 'E:/Other', 'targets': {'t-1': 'auto-x'}, 'wait_seconds': bad}), encoding='utf-8')
        assert guard.load_config(cfg_path)['wait_seconds'] == 1200
    for broken in ['{"targets": {}}', '{"targets": {"t": "a"}, "cwd": " "}', '{"targets": {"t": "a"}, "db_files": {"state": "x"}}']:
        cfg_path.write_text(broken, encoding='utf-8')
        with pytest.raises(ValueError):
            guard.load_config(cfg_path)


def test_status_is_written_with_the_error_when_codex_projections_are_unreadable(tmp_path):
    import json
    cfg = {**guard.CONFIG, 'run_dir': str(tmp_path / 'run')}
    status = guard.run(False, home=tmp_path / 'missing-codex-home', now=1300, config=cfg)
    assert status['threads'] == [] and 'OperationalError' in status['error']
    written = json.loads((tmp_path / 'run' / 'status.json').read_text(encoding='utf-8'))
    assert written['error'] == status['error'] and written['at'] == 1300 and not (tmp_path / 'run' / 'actions.jsonl').exists()


def test_node_comes_from_path_or_the_configured_file(monkeypatch, tmp_path):
    monkeypatch.setattr(guard.shutil, 'which', lambda name: None)
    assert guard.node_path({'node': str(tmp_path / 'absent.exe')}) is None
    present = tmp_path / 'node.exe'
    present.write_text('')
    assert guard.node_path({'node': str(present)}) == str(present)
    monkeypatch.setattr(guard.shutil, 'which', lambda name: '/usr/bin/node')
    assert guard.node_path({'node': None}) == '/usr/bin/node'
