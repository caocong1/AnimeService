"""Synthetic Codex projections only; the collector must never touch a real host."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

import pytest

SPEC = importlib.util.spec_from_file_location('collector', Path(__file__).resolve().parents[1] / 'scripts/codex_absence_verifier.py')
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)

DISPATCHER = 'dispatcher-thread-0001'
ISSUED = 1_760_000_000.0  # seconds
SECRET_TOKEN = 'dispatch-token-SECRET'


def projections(home, history_wal=False, drop_column=None):
    home.mkdir(parents=True, exist_ok=True)
    state = sqlite3.connect(home / 'state_5.sqlite')
    state.executescript('''CREATE TABLE _sqlx_migrations(version BIGINT);
        CREATE TABLE threads(id TEXT, created_at_ms INTEGER, cwd TEXT, first_user_message TEXT, title TEXT);
        CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT);''')
    state.executemany('INSERT INTO _sqlx_migrations VALUES(?)', [(v,) for v in collector.KNOWN_MIGRATIONS['state']])
    state.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (DISPATCHER, int(ISSUED*1000)-100000, 'D:/AnimeService', 'PRIVATE FIRST MESSAGE', 'PRIVATE TITLE'))
    state.commit(); state.close()
    history = sqlite3.connect(home / 'thread_history_1.sqlite')
    if history_wal:
        history.execute('PRAGMA journal_mode=WAL')
    cols = 'thread_id TEXT, turn_id TEXT, status TEXT, started_at REAL, completed_at REAL, rollout_end_ordinal INTEGER, error_json TEXT'
    if drop_column:
        cols = cols.replace(f', {drop_column}', '')
    history.executescript(f'''CREATE TABLE _sqlx_migrations(version BIGINT);
        CREATE TABLE thread_turns({cols});
        CREATE TABLE thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT, item_type TEXT, item_json TEXT, started_at_ms INTEGER, completed_at_ms INTEGER);
        CREATE TABLE thread_history_projection_state(thread_id TEXT, next_rollout_ordinal INTEGER);''')
    history.executemany('INSERT INTO _sqlx_migrations VALUES(?)', [(v,) for v in collector.KNOWN_MIGRATIONS['history']])
    history.commit(); history.close()
    logs = sqlite3.connect(home / 'logs_2.sqlite')
    logs.executescript('CREATE TABLE _sqlx_migrations(version BIGINT); CREATE TABLE logs(id INTEGER PRIMARY KEY, thread_id TEXT, ts REAL);')
    logs.executemany('INSERT INTO _sqlx_migrations VALUES(?)', [(v,) for v in collector.KNOWN_MIGRATIONS['logs']])
    logs.commit(); logs.close()


def config(home):
    return {'codex_home': str(home), 'targets': {DISPATCHER: 'automation-x'}, 'cwd': 'D:/AnimeService',
            'db_files': {'state': 'state_5.sqlite', 'history': 'thread_history_1.sqlite', 'logs': 'logs_2.sqlite'}}


def ticket(action='create', thread=''):
    return {'schema': 'dispatch-ticket/v1', 'job_id': 'episode:42:1', 'revision': 3, 'dispatch_token': SECRET_TOKEN,
            'lease_token': 'lease-token-SECRET', 'owner': DISPATCHER, 'input_key': 'input-1', 'dispatch_action': action,
            'thread_id': thread, 'issued_at': ISSUED}


def turn(home, turn_id='turn-1', status='completed', started=ISSUED + 10, completed=ISSUED + 60, end_ordinal=10, next_ordinal=11, thread=DISPATCHER):
    con = sqlite3.connect(home / 'thread_history_1.sqlite')
    con.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,NULL)', (thread, turn_id, status, started, completed, end_ordinal))
    con.execute('DELETE FROM thread_history_projection_state WHERE thread_id=?', (thread,))
    con.execute('INSERT INTO thread_history_projection_state VALUES(?,?)', (thread, next_ordinal))
    con.commit(); con.close()


def item(home, turn_id, tool, status='completed', error=None, structured=None, raw=None, thread=DISPATCHER):
    body = raw if raw is not None else json.dumps({'type': 'mcpToolCall', 'tool': tool, 'status': status, 'error': error,
                                                   'arguments': {'message': 'PRIVATE MESSAGE BODY'}, 'result': {'content': 'PRIVATE RESULT TEXT', 'structuredContent': structured}})
    con = sqlite3.connect(home / 'thread_history_1.sqlite')
    con.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)', (thread, turn_id, f'item-{tool}-{status}', 'mcpToolCall', body, int(ISSUED * 1000) + 20_000, int(ISSUED * 1000) + 25_000))
    con.commit(); con.close()


def test_exact_binding_reports_the_screening_without_ever_authorizing_absence(tmp_path):
    home = tmp_path / 'codex'; projections(home)
    turn(home); item(home, 'turn-1', 'send_message_to_thread', status='failed', error={'message': 'PRIVATE ERROR'})
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    assert code == 0 and report['diagnostic_only'] is True and report['absence_authorized'] is False
    assert report['host_ref_binding'] == 'exact_locator_not_dispatch_verified' and report['screening']['turn_ended'] is True
    assert report['ticket_host_binding'] == 'unverified'
    assert report['screening']['watermark_consumed'] is True and report['screening']['calls_without_thread_result'] is True
    assert report['screening']['marker_check'] == 'not_performed' and report['screening']['absence_authorized'] is False
    assert 'host_operation_terminal_api_unavailable' in report['gaps'] and 'marker_check_not_performed' in report['gaps']
    dump = json.dumps(report)
    for secret in (SECRET_TOKEN, 'lease-token-SECRET', 'PRIVATE MESSAGE BODY', 'PRIVATE RESULT TEXT', 'PRIVATE ERROR', DISPATCHER, 'turn-1', 'episode:42:1'):
        assert secret not in dump
    assert report['job_digest'] == hashlib.sha256(b'episode:42:1').hexdigest()


def test_missing_host_ref_degrades_to_window_candidates_and_keeps_turns_started_before_begin(tmp_path):
    home = tmp_path / 'codex'; projections(home)
    turn(home, 'early', status='inProgress', started=ISSUED - 50, completed=None)
    turn(home, 'old', status='completed', started=ISSUED - 500, completed=ISSUED - 400)
    report, code = collector.collect(config(home), ticket(), '', now=ISSUED + 100)
    assert 'host_ref_unbound' in report['gaps'] and report['host_ref_binding'] == 'unbound'
    turns = [o for o in report['observations'] if o['kind'] == 'turn']
    assert [t['started_before_begin'] for t in turns] == [True] and turns[0]['status'] == 'inProgress'
    assert report['screening']['turn_ended'] is False and code == 0


def test_forged_or_unknown_host_refs_are_reported_not_trusted(tmp_path):
    home = tmp_path / 'codex'; projections(home); turn(home)
    report, code = collector.collect(config(home), ticket(), 'thread:someone-else/turn:turn-1', now=ISSUED + 100)
    assert code == collector.INCOMPLETE and 'thread_outside_dispatcher_scope' in report['gaps']
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:never-happened', now=ISSUED + 100)
    assert 'turn_not_found' in report['gaps'] and 'no_candidate_turn' in report['gaps'] and report['screening']['turn_ended'] is None
    assert collector.parse_host_ref('turn:only') == {} and collector.parse_host_ref('thread:a/thread:b') == {}


def test_units_are_declared_and_anomalies_fail_closed(tmp_path):
    home = tmp_path / 'codex'; projections(home)
    turn(home, started=ISSUED * 1000, completed=ISSUED * 1000 + 5)  # milliseconds where seconds are declared
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    assert 'unit_anomaly:thread_turns.started_at' in report['gaps']
    assert code == collector.INCOMPLETE
    assert [o for o in report['observations'] if o['kind'] == 'turn'][0]['started_before_begin'] is None
    assert report['units']['thread_turns.started_at'] == 'seconds' and report['units']['threads.created_at_ms'] == 'milliseconds'


def test_thread_results_and_reuse_are_reported_as_candidates_only(tmp_path):
    home = tmp_path / 'codex'; projections(home); turn(home)
    item(home, 'turn-1', 'send_message_to_thread', structured={'threadId': 'responsible-thread-7'})
    report, code = collector.collect(config(home), ticket('reuse', 'responsible-thread-7'), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    calls = [o for o in report['observations'] if o['kind'] == 'turn'][0]['create_or_reuse_calls']
    assert calls[0]['thread_result'] is True and calls[0]['matches_ticket_thread'] is True and calls[0]['structured_type'] == 'object'
    assert report['screening']['calls_without_thread_result'] is False and report['absence_authorized'] is False
    assert 'responsible-thread-7' not in json.dumps(report)
    home2 = tmp_path / 'codex2'; projections(home2); turn(home2)
    item(home2, 'turn-1', 'create_thread', structured={})
    report, _ = collector.collect(config(home2), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    assert [o for o in report['observations'] if o['kind'] == 'turn'][0]['call_summary'] == 'no_thread_result'


def test_orphan_turns_and_projection_lag_are_visible(tmp_path):
    home = tmp_path / 'codex'; projections(home)
    turn(home, status='inProgress', completed=None, end_ordinal=10, next_ordinal=8)
    report, _ = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    obs = [o for o in report['observations'] if o['kind'] == 'turn'][0]
    assert obs['watermark'] == 'lagging' and report['screening']['turn_ended'] is False and report['screening']['watermark_consumed'] is False


def test_spawn_edges_and_cross_database_gaps(tmp_path):
    home = tmp_path / 'codex'; projections(home); turn(home)
    con = sqlite3.connect(home / 'state_5.sqlite')
    con.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (DISPATCHER, 'child-after', 'open'))
    con.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (DISPATCHER, 'child-missing', 'closed'))
    con.execute("INSERT INTO threads VALUES(?,?,?,?,?)", ('child-after', int(ISSUED * 1000) + 5000, 'D:/AnimeService', 'PRIVATE FIRST MESSAGE', 'PRIVATE TITLE'))
    con.commit(); con.close()
    report, _ = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    assert report['screening']['spawn_children_after_issued'] == 1 and report['screening']['spawn_edges_examined'] == 2
    assert 'child_thread_missing' in report['gaps'] and 'PRIVATE' not in json.dumps(report) and 'child-after' not in json.dumps(report)


def test_unknown_schema_bad_json_and_unreadable_databases_are_incomplete(tmp_path, monkeypatch):
    home = tmp_path / 'codex'; projections(home, drop_column='rollout_end_ordinal INTEGER')
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    assert code == collector.INCOMPLETE and any(g.startswith('unsupported_schema:history:') and 'rollout_end_ordinal' in g for g in report['gaps'])
    assert report['schema_versions']['history'] == list(collector.KNOWN_MIGRATIONS['history']) and report['coverage']['databases']['history']['opened'] is False
    home2 = tmp_path / 'codex2'; projections(home2); turn(home2)
    item(home2, 'turn-1', 'create_thread', raw='{not json')
    report, code = collector.collect(config(home2), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    assert 'item_json_malformed' in report['gaps']
    assert code == collector.INCOMPLETE
    assert report['screening']['calls_without_thread_result'] is False
    home3 = tmp_path / 'codex3'; projections(home3); turn(home3)
    original = collector.open_ro
    def denied(path, *args):
        if Path(path).name == 'thread_history_1.sqlite':
            raise PermissionError('PRIVATE PATH MUST NOT APPEAR')
        return original(path, *args)
    monkeypatch.setattr(collector, 'open_ro', denied)
    report, code = collector.collect(config(home3), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED + 100)
    assert code == collector.INCOMPLETE and any(g.startswith('database_unavailable:history') for g in report['gaps'])


def test_row_limits_and_time_limits_mark_the_report_incomplete(tmp_path):
    home = tmp_path / 'codex'; projections(home)
    for n in range(3):
        turn(home, f'turn-{n}', started=ISSUED + n, completed=ISSUED + 50 + n)
    report, code = collector.collect(config(home), ticket(), '', now=ISSUED + 100, max_rows=2)
    assert code == collector.INCOMPLETE and report['coverage']['truncated'] is True and report['screening']['turns_examined'] == 2
    ticks = iter([0, 0, 100, 100, 100, 100, 100, 100])
    report, code = collector.collect(config(home), ticket(), '', now=ISSUED + 100, time_limit=1, clock=lambda: next(ticks, 100))
    assert code == collector.INCOMPLETE and 'time_limit_exceeded' in report['gaps']


def test_wal_committed_rows_are_visible_and_the_source_is_not_modified(tmp_path):
    home = tmp_path / 'codex'; projections(home, history_wal=True)
    writer = sqlite3.connect(home / 'thread_history_1.sqlite')
    writer.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,NULL)', (DISPATCHER, 'turn-wal', 'completed', ISSUED + 1, ISSUED + 2, 3))
    writer.execute('INSERT INTO thread_history_projection_state VALUES(?,?)', (DISPATCHER, 4))
    writer.commit()  # stays in the WAL side file; no checkpoint
    before = {p.name: p.stat().st_size for p in home.iterdir()}
    content_before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir() if p.suffix == '.sqlite' or p.name.endswith('-wal')}
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-wal', now=ISSUED + 100)
    assert code == 0 and report['screening']['turns_examined'] == 1 and report['screening']['watermark_consumed'] is True
    assert {p.name: p.stat().st_size for p in home.iterdir()} == before
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in home.iterdir() if p.suffix == '.sqlite' or p.name.endswith('-wal')} == content_before
    writer.close()
    after = {p.name: p.stat().st_size for p in home.iterdir()}
    assert after['thread_history_1.sqlite'] == before['thread_history_1.sqlite']


def test_reports_are_immutable_hash_bound_files_and_the_cli_demands_dry_run(tmp_path, capsys):
    from scripts.subtitle_delivery_queue import protect_ticket_directory
    protect_ticket_directory(tmp_path)
    home = tmp_path / 'codex'; projections(home); turn(home)
    cfg = tmp_path / 'dispatch.json'; cfg.write_text(json.dumps(config(home)), encoding='utf-8')
    tk = tmp_path / 'ticket.json'; tk.write_text(json.dumps(ticket()), encoding='utf-8')
    out = tmp_path / 'report.json'
    with pytest.raises(SystemExit) as stop:
        collector.main(['--ticket', str(tk), '--out', str(out), '--config', str(cfg)])
    assert stop.value.code == collector.USAGE
    code = collector.main(['--dry-run', '--ticket', str(tk), '--out', str(out), '--config', str(cfg), '--host-ref', f'thread:{DISPATCHER}/turn:turn-1'])
    printed = json.loads(capsys.readouterr().out)
    data = out.read_bytes()
    assert code == 0 and printed['absence_authorized'] is False and printed['sha256'] == hashlib.sha256(data).hexdigest()
    assert out.with_name('report.json.sha256').read_text().strip() == printed['sha256']
    assert json.loads(data)['absence_authorized'] is False and SECRET_TOKEN not in data.decode('utf-8')
    with pytest.raises(SystemExit):
        collector.main(['--dry-run', '--ticket', str(tk), '--out', str(out), '--config', str(cfg)])  # immutable: refuses to overwrite


def test_schema_version_change_is_rejected_before_reading_application_rows(tmp_path):
    home = tmp_path/'codex'; projections(home); turn(home)
    with sqlite3.connect(home/'thread_history_1.sqlite') as con:
        con.execute('INSERT INTO _sqlx_migrations VALUES(999)')
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED+100)
    assert code == 3 and any('unknown_migration_set' in gap for gap in report['gaps'])
    assert not any(o['kind'] == 'turn' for o in report['observations'])


def test_configured_dispatcher_must_also_match_the_project_cwd(tmp_path):
    home = tmp_path/'codex'; projections(home); turn(home)
    with sqlite3.connect(home/'state_5.sqlite') as con:
        con.execute('UPDATE threads SET cwd=? WHERE id=?', ('D:/OtherProject', DISPATCHER))
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED+100)
    assert code == 3 and 'dispatcher_cwd_unverified' in report['gaps']
    assert not any(o['kind'] == 'turn' for o in report['observations'])


def test_item_locator_is_checked_and_never_claims_authenticated_dispatch_binding(tmp_path):
    home = tmp_path/'codex'; projections(home); turn(home); item(home, 'turn-1', 'create_thread')
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1/item:missing', now=ISSUED+100)
    assert code == 3 and 'item_not_found_or_wrong_tool' in report['gaps']
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1/item:item-create_thread-completed', now=ISSUED+100)
    assert code == 0 and report['host_ref_binding'] == 'exact_locator_not_dispatch_verified'
    assert report['ticket_host_binding'] == 'unverified' and not report['absence_authorized']
    assert collector.parse_host_ref('thread:x/item:y') == {}
    assert collector.parse_host_ref('turn:y/thread:x') == {}
    report, code = collector.collect(config(home), ticket(), 'not-a-ref', now=ISSUED+100)
    assert code == 3 and 'host_ref_invalid' in report['gaps']


def test_global_rows_and_completion_timestamps_are_bounded(tmp_path):
    home = tmp_path/'codex'; projections(home); turn(home)
    for tool in ('create_thread','send_message_to_thread'):
        item(home, 'turn-1', tool)
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED+100, max_rows=2)
    assert code == 3 and report['coverage']['truncated'] and report['coverage']['rows_examined'] == 2
    with sqlite3.connect(home/'thread_history_1.sqlite') as con:
        con.execute('UPDATE thread_items SET completed_at_ms=1')
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED+100)
    assert code == 3 and 'unit_anomaly:thread_items.completed_at_ms' in report['gaps']


def test_sql_work_is_actually_cancelled_by_the_progress_handler(tmp_path):
    home = tmp_path/'codex'; projections(home)
    budget = collector.Budget(0.05, 200)
    con = collector.open_ro(home/'state_5.sqlite', 0, budget)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError, match='interrupted'):
            con.execute('WITH RECURSIVE x(n) AS (VALUES(0) UNION ALL SELECT n+1 FROM x WHERE n<100000000) SELECT sum(n) FROM x').fetchone()
        assert budget.exhausted and time.monotonic()-started < 2
    finally:
        con.set_progress_handler(None,0); con.close()


@pytest.mark.parametrize('seconds,rows', [(float('nan'),200), (float('inf'),200), (0,200), (31,200), (20,0), (20,1001)])
def test_unbounded_limits_are_rejected_before_opening_a_database(seconds, rows, tmp_path):
    with pytest.raises(ValueError):
        collector.collect(config(tmp_path/'missing'), ticket(), '', time_limit=seconds, max_rows=rows)


def test_unknown_status_and_ordinal_values_cannot_leak_arbitrary_text(tmp_path):
    home = tmp_path/'codex'; projections(home); turn(home)
    item(home, 'turn-1', 'create_thread', status='SECRET_STATUS')
    with sqlite3.connect(home/'thread_history_1.sqlite') as con:
        con.execute('UPDATE thread_turns SET status=?, rollout_end_ordinal=?', ('SECRET_TURN', 'SECRET_ORDINAL'))
    report, code = collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED+100)
    assert code == 3 and 'SECRET_' not in json.dumps(report)


def test_existing_sidecar_is_not_overwritten_and_report_size_is_bounded(tmp_path):
    from scripts.subtitle_delivery_queue import protect_ticket_directory
    protect_ticket_directory(tmp_path)
    out = tmp_path/'report.json'; sidecar=tmp_path/'report.json.sha256'
    sidecar.write_text('existing', encoding='utf-8')
    with pytest.raises(FileExistsError):
        collector.write_report({'diagnostic_only':True,'absence_authorized':False}, out)
    assert not out.exists() and sidecar.read_text() == 'existing'
    with pytest.raises(ValueError, match='size limit'):
        collector.write_report({'payload':'x'*collector.MAX_REPORT_BYTES}, tmp_path/'large.json')


def test_default_invalid_config_never_falls_back_to_scanning_default_targets(tmp_path, monkeypatch, capsys):
    from scripts.subtitle_delivery_queue import protect_ticket_directory
    protect_ticket_directory(tmp_path)
    cfg=tmp_path/'bad.json'; cfg.write_text('{"SECRET_CONFIG":',encoding='utf-8')
    tk=tmp_path/'ticket.json'; tk.write_text(json.dumps(ticket()),encoding='utf-8')
    monkeypatch.setenv('ANIMESERVICE_DISPATCH_CONFIG', str(cfg))
    with pytest.raises(SystemExit) as stop:
        collector.main(['--dry-run','--ticket',str(tk),'--out',str(tmp_path/'out.json')])
    assert stop.value.code == 2 and not (tmp_path/'out.json').exists()
    assert 'SECRET_CONFIG' not in capsys.readouterr().err


def test_non_string_thread_results_are_incomplete_and_never_exposed(tmp_path):
    home=tmp_path/'codex'; projections(home); turn(home)
    item(home, 'turn-1', 'create_thread', structured={'threadId': {'PRIVATE_BODY': 'SECRET'}})
    report, code=collector.collect(config(home), ticket(), f'thread:{DISPATCHER}/turn:turn-1', now=ISSUED+100)
    assert code == 3 and 'item_thread_result_invalid' in report['gaps']
    assert 'PRIVATE_BODY' not in json.dumps(report) and 'SECRET' not in json.dumps(report)
