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
