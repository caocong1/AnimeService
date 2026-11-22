import pytest

from test_subtitle_delivery import delivery
from test_subtitle_preparation import media, jobs
from anime.subtitle_preparation import preparation_plan
from anime.subtitle_recovery import recovery_plan, record_attempt, update_monitor, DEADLINE, COOLDOWN, EXECUTION_STALL


def plan(db, rows, now=2000):
    return recovery_plan(db, rows, preparation_plan(db, rows, now=now), now=now)


def test_native_chinese_routes_require_live_matching_material_and_keep_other_work():
    from anime.subtitle_recovery import independent_chinese_routes
    actions = ['collect_chinese_source', 'align_chinese_source', 'review_chinese_ocr', 'find_chinese']
    draft = dict(kind='archived_draft', origin='embedded', media_fingerprint='bound',
                 cue_count=345, text_hash='archived')
    source = dict(actions=actions, candidates=[draft])
    assessment = dict(material_ready=True, text_hashes=['archived'],
                      material_refs=[dict(kind='completed_media', target=dict(fingerprint='bound'))])
    assert independent_chinese_routes(source, assessment) == ['collect_chinese_source', 'review_chinese_ocr']
    for change in (dict(media_fingerprint='other'), dict(estimated=True), dict(mixed=True),
                   dict(candidate_not_reliable_base=True), dict(cue_count=0), dict(text_hash='new'),
                   dict(target_matching_status='alignment_pending')):
        assert independent_chinese_routes(dict(source, candidates=[dict(draft, **change)]), assessment) == actions
    assert independent_chinese_routes(source, dict(assessment, material_ready=False)) == actions
    source['candidates'].append(dict(kind='archived_draft', correction_progress={'source_hardtext_review': 'complete'}))
    assert independent_chinese_routes(source, assessment) == actions[:-1]


def test_legacy_chinese_budget_survives_technical_gate_closure(delivery, tmp_path, monkeypatch):
    import hashlib
    import json
    import anime.subtitle_chinese as chinese
    from anime.subtitle_recovery import condition, archive
    original = chinese.chinese_preparation
    def search_only(*args, **kwargs):
        result = original(*args, **kwargs)
        result['actions'] = ['find_chinese']
        return result
    monkeypatch.setattr(chinese, 'chinese_preparation', search_only)
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    assessment = rows[0]['payload']['assessment']
    assessment['capability_ready'] = False
    assessment['reasons'].append('needs_verified_local_finished_output_tools')
    evidence = db.path.parent / 'subtitle-studio' / 'legacy-search.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"search":"no qualified Chinese"}')
    current = plan(db, rows)
    primary = current['items'][0]
    route = primary['independent_routes'][0]
    receipt = record_attempt(db, current, primary['job_id'], 'find_chinese', 'failed', [evidence], now=2000)
    # Simulate a receipt written by the old deployed planner, not production data.
    old_condition = hashlib.sha256(json.dumps([condition(rows[0], assessment),
        'autonomous-chinese-v1', route['source_revision']], sort_keys=True).encode()).hexdigest()
    path = next((archive(db) / 'attempts').glob('*.json'))
    receipt['condition'] = old_condition
    receipt.pop('source_revision', None)
    path.write_text(json.dumps(receipt))
    assessment['capability_ready'] = True
    assessment['reasons'].remove('needs_verified_local_finished_output_tools')
    after = plan(db, rows, 2001)
    row = after['items'][0]
    assert row['action'] == 'find_chinese'
    assert row['failed_attempts'] == 1 and row['status'] == 'retry_wait'
    assert record_attempt(db, after, row['job_id'], 'find_chinese', 'failed', [evidence], now=2001) == receipt
    evidence.write_text('{"search":"second real search"}')
    record_attempt(db, plan(db, rows, 3800), row['job_id'], 'find_chinese', 'failed', [evidence], now=3800)
    assert plan(db, rows, 3801)['items'][0]['status'] == 'source_wait'
    rows[0]['input_key'] = 'different-fixed-input'
    assert plan(db, rows, 3801)['items'][0]['failed_attempts'] == 0


def test_chinese_journal_during_output_cooldown_preserves_both_budgets(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    rows[0]['payload']['assessment']['capability_ready'] = False
    evidence = db.path.parent / 'subtitle-studio' / 'independent-search.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"decode":"timeout"}')
    first = plan(db, rows)['next_action']
    assert first['action'] == 'verify_output'
    record_attempt(db, plan(db, rows), first['job_id'], 'verify_output', 'failed', [evidence], now=2000)
    evidence.write_text('{"search":"no qualified Chinese source"}')
    current = plan(db, rows, 2001)
    route = next(r for r in current['items'][0]['independent_routes'] if r['action'] == 'find_chinese')
    for input_key, condition in (('stale-input', route['condition']), (route['input_key'], 'stale-condition')):
        with pytest.raises(ValueError, match='input or condition changed'):
            record_attempt(db, current, first['job_id'], 'find_chinese', 'failed', [evidence], now=2001,
                           expected_input=input_key, expected_condition=condition)
    assert not plan(db, rows, 2001)['items'][0]['independent_routes'][0]['failed_attempts']
    record_attempt(db, current, first['job_id'], 'find_chinese', 'failed', [evidence], now=2001,
                   expected_input=route['input_key'], expected_condition=route['condition'])
    current = plan(db, rows, 2002)
    primary = next(r for r in current['items'] if r['job_id'] == first['job_id'])
    assert primary['status'] == 'retry_wait' and primary['failed_attempts'] == 1
    search = next(r for r in primary['independent_routes'] if r['action'] == 'find_chinese')
    assert search['status'] == 'retry_wait' and search['failed_attempts'] == 1
    evidence.write_text('{"search":"different evidence same cooldown"}')
    with pytest.raises(ValueError, match='cooling down'):
        record_attempt(db, current, first['job_id'], 'find_chinese', 'failed', [evidence], now=2002)
    rows[0]['dispatch_state'] = 'unknown'
    protected = plan(db, rows, 2002)
    assert not protected['items'][0].get('independent_routes')
    with pytest.raises(ValueError, match='protected'):
        record_attempt(db, protected, first['job_id'], 'find_chinese', 'failed', [evidence], now=2002)


@pytest.mark.parametrize('outcome, primary_status', [('failed', 'retry_wait'), ('blocked', 'repair_blocked')])
def test_independent_chinese_route_remains_next_action_and_monitored(
        delivery, tmp_path, monkeypatch, outcome, primary_status):
    from anime.subtitle_delivery import SubtitleDelivery

    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    rows[0]['payload']['assessment']['capability_ready'] = False
    evidence = db.path.parent / 'subtitle-studio' / 'output-result.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"decode":"output dependency unavailable"}')
    initial = plan(db, rows)
    job_id = initial['next_action']['job_id']
    record_attempt(db, initial, job_id, 'verify_output', outcome, [evidence], now=2000)
    current = plan(db, rows, 2001)
    primary = next(row for row in current['items'] if row['job_id'] == job_id)
    assert primary['status'] == primary_status and primary['failed_attempts'] == (outcome == 'failed')
    assert current['next_action'] in primary['independent_routes']
    assert current['next_action']['job_id'] == job_id
    assert current['next_action']['input_key'] == primary['input_key']
    assert current['next_action']['status'] == 'actionable'
    assert current['next_action']['action'] == 'collect_chinese_source'

    # The existing guard must see this independent work even while the primary
    # gate remains unavailable. A repeated observation does not reset its clock.
    now = [2001.0]
    monkeypatch.setattr('anime.subtitle_recovery.time.time', lambda: now[0])
    def snapshot(self):
        return {'recovery': {**current, 'checked_at': now[0]}}
    monkeypatch.setattr(SubtitleDelivery, 'snapshot', snapshot)
    first = update_monitor(db.path)
    assert first['execution']['status'] == 'pending'
    assert first['execution']['action'] == current['next_action']['action']
    current['next_action'] = {**current['next_action'], 'action': 'find_chinese',
                              'condition': 'route-selection-changed-without-work'}
    now[0] += EXECUTION_STALL
    second = update_monitor(db.path)
    assert second['execution']['status'] == 'stalled'
    assert second['execution']['stalled_since'] == 2001
    assert primary['status'] == primary_status  # no gate or owner mutation


def test_actionable_deadline_ignores_refresh_and_still_attempts_overdue(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    result = plan(db, rows)
    row = next(r for r in result['items'] if r['show_id'] == 42 and r['episode'] == 1)
    assert row['status'] == 'actionable' and row['action'] == 'collect_chinese_source'
    rows[0]['updated'] = 50000
    overdue = plan(db, rows, now=50000)
    row2 = next(r for r in overdue['items'] if r['job_id'] == row['job_id'])
    assert row2['deadline_at'] == row['deadline_at'] and row2['status'] == 'overdue'
    assert overdue['next_action']['job_id'] == row['job_id']  # timeout escalates, but does not skip repair


def test_overdue_source_work_precedes_a_new_output_check(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path, 42, 1)
    media(db, tmp_path, 43, 1)
    db.execute('UPDATE subtitle_studio_media SET updated=29000 WHERE show_id=43')
    rows = jobs(scheduler)
    newer = next(row for row in rows if row['show_id'] == 43 and row['episode'] == 1)
    newer['payload']['assessment']['capability_ready'] = False
    current = plan(db, rows, now=30000)
    assert next(row for row in current['items'] if row['job_id'] == newer['id'])['status'] == 'actionable'
    assert current['next_action']['job_id'] == 'episode:42:1'
    assert current['next_action']['status'] == 'overdue'


def test_real_failures_cooldown_dedupe_and_limit_do_not_block_other_jobs(delivery, tmp_path, monkeypatch):
    import anime.subtitle_chinese as chinese
    original = chinese.chinese_preparation
    def one_route(*args, **kwargs):
        result = original(*args, **kwargs)
        result['actions'] = [result['action']]
        return result
    monkeypatch.setattr(chinese, 'chinese_preparation', one_route)
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    media(db, tmp_path, 43, 1)
    rows = jobs(scheduler)
    evidence = db.path.parent / 'subtitle-studio' / 'repair.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"exit_code":1}')
    first = plan(db, rows)['next_action']
    result = record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'failed', [evidence], now=2000)
    assert record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'failed', [evidence], now=2001) == result
    waiting = plan(db, rows, 2001)
    row = next(r for r in waiting['items'] if r['job_id'] == first['job_id'])
    assert row['status'] == 'retry_wait' and row['failed_attempts'] == 1 and row['next_check_at'] == 2000 + COOLDOWN
    assert waiting['next_action']['show_id'] == 43
    later = plan(db, rows, 2000 + COOLDOWN)
    assert next(r for r in later['items'] if r['job_id'] == first['job_id'])['status'] == 'actionable'
    evidence.write_text('{"exit_code":2}')
    record_attempt(db, later, first['job_id'], first['action'], 'failed', [evidence], now=2000 + COOLDOWN)
    exhausted = plan(db, rows, 2001 + COOLDOWN)
    row = next(r for r in exhausted['items'] if r['job_id'] == first['job_id'])
    assert row['status'] == 'source_wait' and row['failed_attempts'] == 2
    assert row['user_action'] is None and not row['notification_pending']
    assert exhausted['next_action']['show_id'] == 43
    assert plan(db, rows, 3 * 86400)['items'][0]['failed_attempts'] == 2  # no daily infinite reset


def test_new_input_resets_attempts_but_observation_does_not(delivery, tmp_path, monkeypatch):
    import anime.subtitle_chinese as chinese
    monkeypatch.setattr(chinese, 'chinese_preparation', lambda *a, **kw: {
        'action': 'collect_chinese_source', 'actions': ['collect_chinese_source'],
        'revision': 'same-source', 'candidates': []})
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    evidence = db.path.parent / 'subtitle-studio' / 'e.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{}')
    first = plan(db, rows)['next_action']
    record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'failed', [evidence], now=2000)
    rows[0]['revision'] += 1
    assert plan(db, rows, 2001)['items'][0]['failed_attempts'] == 1
    rows[0]['input_key'] = 'genuinely-new-input'
    assert plan(db, rows, 2001)['items'][0]['failed_attempts'] == 0


@pytest.mark.parametrize('patch', [ {'dispatch_state': 'acknowledged'}, {'dispatch_state': 'committing'},
    {'dispatch_state': 'unknown'}, {'thread_id': 'held-owner'}, {'scope_blocker': 'authorization'},
    {'lease_expires': 99999}, {'finished': True}, {'delivery_finished': True}])
def test_protections_never_become_automatic_repair(delivery, tmp_path, patch):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    rows[0].update(patch)
    assert not plan(db, rows, 50000)['next_action']


def test_missing_media_not_a_failure_or_new_download_permission(delivery):
    db, scheduler, _, _ = delivery
    result = plan(db, jobs(scheduler), 50000)
    assert result['summary']['waiting_media'] == 4 and not result['next_action']


def test_stopped_engineering_dependency_gets_real_repair_route_without_resuming(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    job = rows[0]
    job.update(state='blocked', dispatch_state='blocked-stopped', thread_id='original-owner')
    job['payload'].update(owner_stopped_writing=True, recovery_class='repair_blocked', recovery_since=1500,
                          production_blockers=['v1 issue archive merge unavailable'])
    first = plan(db, rows, 50000)
    repair = first['next_action']
    assert repair['job_id'] == job['id'] and repair['action'] == 'repair_owner_dependency'
    assert repair['domain'] == 'owner_recovery' and repair['status'] == 'overdue'
    evidence = db.path.parent / 'subtitle-studio' / 'dependency.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"actual_error":"no safe merge entry"}', encoding='utf-8')
    record_attempt(db, first, job['id'], repair['action'], 'failed', [evidence], now=50000)
    waiting = next(r for r in plan(db, rows, 50001)['items'] if r['job_id'] == job['id'])
    assert waiting['status'] == 'retry_wait' and waiting['failed_attempts'] == 1
    evidence.write_text('{"actual_error":"second real attempt failed"}', encoding='utf-8')
    record_attempt(db, plan(db, rows, 50000 + COOLDOWN), job['id'], repair['action'], 'failed',
                   [evidence], now=50000 + COOLDOWN)
    exhausted = next(r for r in plan(db, rows, 50001 + COOLDOWN)['items'] if r['job_id'] == job['id'])
    assert exhausted['status'] == 'repair_blocked' and exhausted['failed_attempts'] == 2
    assert job['dispatch_state'] == 'blocked-stopped' and job['payload']['owner_stopped_writing']
    assert job['payload']['recovery_class'] == 'repair_blocked'
    job['payload']['user_stopped'] = True
    protected = next(r for r in plan(db, rows, 90000)['items'] if r['job_id'] == job['id'])
    assert protected['status'] == 'protected' and protected['action'] is None


def test_corrupt_attempt_is_visible_and_does_not_break_status(delivery):
    db, scheduler, _, _ = delivery
    path = db.path.parent / 'subtitle-studio/recovery/attempts/broken.json'
    path.parent.mkdir(parents=True)
    path.write_text('{')
    result = scheduler.snapshot()
    assert result['jobs'] and result['recovery']['error'] == 'recovery_journal_invalid'


def test_evidence_outside_private_archive_is_rejected_and_business_db_unchanged(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    before = db.rows('SELECT * FROM subtitle_delivery_jobs'), db.rows('SELECT * FROM watches')
    rows = jobs(scheduler)
    first = plan(db, rows)['next_action']
    evidence = tmp_path / 'outside.json'
    evidence.write_text('{}')
    with pytest.raises(ValueError):
        record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'failed', [evidence])
    assert before == (db.rows('SELECT * FROM subtitle_delivery_jobs'), db.rows('SELECT * FROM watches'))


def test_subtitle_material_exhaustion_never_requests_human_work(delivery, tmp_path, monkeypatch):
    import anime.subtitle_chinese as chinese
    original = chinese.chinese_preparation
    def one_route(*args, **kwargs):
        result = original(*args, **kwargs)
        result['actions'] = [result['action']]
        return result
    monkeypatch.setattr(chinese, 'chinese_preparation', one_route)
    from anime.subtitle_recovery import acknowledge_notice
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    evidence = db.path.parent / 'subtitle-studio/blocked.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"error":"no controlled repair endpoint"}')
    first = plan(db, rows)['next_action']
    record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'blocked', [evidence], now=2000)
    blocked = plan(db, rows, 2001)
    row = next(r for r in blocked['items'] if r['job_id'] == first['job_id'])
    assert row['status'] == 'source_wait' and not row['notification_pending']
    with pytest.raises(ValueError): acknowledge_notice(db, blocked, 'stale-key')
    with pytest.raises(ValueError): acknowledge_notice(db, blocked, row['notification_key'])


def test_unknown_dispatch_notice_does_not_authorize_retry(delivery, tmp_path):
    from anime.subtitle_recovery import acknowledge_notice
    db, scheduler, _, _ = delivery
    rows = jobs(scheduler)
    rows[0].update(dispatch_state='unknown', updated=1)
    current = plan(db, rows, DEADLINE + 2)
    row = next(r for r in current['items'] if r['job_id'] == rows[0]['id'])
    assert row['status'] == 'reconciling' and row['action'] is None
    assert not row['notification_pending'] and row['user_action'] is None
    assert not next(r for r in plan(db, rows, DEADLINE + 3)['items'] if r['job_id'] == rows[0]['id'])['notification_pending']


def test_new_ocr_route_and_source_revision_escape_search_failures_without_refresh(delivery, tmp_path, monkeypatch):
    import anime.subtitle_chinese as chinese
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    source = {'action': 'find_chinese', 'revision': 'original-source', 'candidates': []}
    monkeypatch.setattr(chinese, 'chinese_preparation', lambda *a, **kw: dict(source))
    evidence = db.path.parent / 'subtitle-studio/search-failed.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"result":"no standalone subtitle"}')
    first = plan(db, rows)['next_action']
    record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'blocked', [evidence], now=2000)
    assert plan(db, rows, 2001)['items'][0]['status'] == 'source_wait'
    source.update(action='review_chinese_ocr', revision='ocr-source')
    changed = plan(db, rows, 2002)['items'][0]
    assert changed['status'] == 'actionable' and changed['action'] == 'review_chinese_ocr'
    assert changed['failed_attempts'] == 0 and changed['user_action'] is None
    record_attempt(db, plan(db, rows, 2002), first['job_id'], changed['action'], 'failed', [evidence], now=2002)
    assert plan(db, rows, 2003)['items'][0]['status'] == 'retry_wait'
    rows[0]['revision'] += 1
    assert plan(db, rows, 2004)['items'][0]['failed_attempts'] == 1
    source['revision'] = 'corrected-source'
    assert plan(db, rows, 2004)['items'][0]['failed_attempts'] == 0


def test_failed_collection_automatically_falls_back_to_existing_ocr(delivery, tmp_path, monkeypatch):
    import anime.subtitle_chinese as chinese
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    monkeypatch.setattr(chinese, 'chinese_preparation', lambda *a, **kw: {
        'action': 'collect_chinese_source', 'actions': ['collect_chinese_source', 'review_chinese_ocr'],
        'revision': 'same-existing-sources', 'candidates': []})
    evidence = db.path.parent / 'subtitle-studio/collection-blocked.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"error":"unsupported source format"}')
    first = plan(db, rows)['next_action']
    record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'blocked', [evidence], now=2000)
    fallback = plan(db, rows, 2001)['items'][0]
    assert fallback['status'] == 'actionable' and fallback['action'] == 'review_chinese_ocr'
    assert fallback['failed_attempts'] == 0 and fallback['condition'] == first['condition']
    record_attempt(db, plan(db, rows, 2001), first['job_id'], fallback['action'], 'blocked', [evidence], now=2001)
    assert plan(db, rows, 2002)['items'][0]['status'] == 'source_wait'


def test_manual_stop_protected_but_registered_runtime_failure_is_repairable(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    rows[0].update(dispatch_state='blocked-stopped', thread_id='original-owner')
    rows[0]['payload']['owner_stopped_writing'] = True
    rows[0]['payload']['production_blockers'] = ['explicit user stop']
    assert not plan(db, rows)['next_action']
    rows[0]['payload']['production_blockers'] = ['11436 refused connection; Resume same owner only on new usable audio']
    assert not plan(db, rows)['next_action']  # legacy prose alone is not recovery authorization
    rows[0]['payload'].update(recovery_class='system', recovery_turn='origin-turn-1')
    assert plan(db, rows)['next_action']['action'] == 'recover_owner'


def test_read_only_recovery_does_not_import_the_web_application(delivery, tmp_path, monkeypatch):
    import sys
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    # The guard runs outside the web venv. Any read-path import of the FastAPI
    # studio module must fail this test; the independent core remains available.
    monkeypatch.setitem(sys.modules, 'anime.subtitle_studio', None)
    result = plan(db, rows)
    assert result['next_action']['action'] == 'collect_chinese_source'


def test_user_stop_wins_even_over_legacy_audio_recovery_text(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    rows[0].update(dispatch_state='blocked-stopped', thread_id='original-owner')
    rows[0]['payload'].update(owner_stopped_writing=True, user_stopped=True,
        production_blockers=['11436 refused connection; Resume same owner only on new usable audio'])
    row = plan(db, rows)['items'][0]
    assert row['status'] == 'protected' and row['reason'] == 'user_stopped'
    assert not plan(db, rows)['next_action']


def test_unfinished_v1_refinement_is_not_skipped_after_v0_delivery(delivery):
    db, scheduler, _, _ = delivery
    rows = scheduler.snapshot()['jobs']
    row = rows[0]
    row.update(kind='refinement', delivery_finished=True, state='blocked',
               dispatch_state='blocked-stopped', thread_id='original-owner')
    row['payload'].update(owner_stopped_writing=True, recovery_class='system', recovery_turn='turn-v1')
    row['payload']['assessment']['ready'] = True
    result = plan(db, rows)
    assert result['next_action']['job_id'] == row['id']
    assert result['next_action']['domain'] == 'owner_recovery'


def test_missing_inventory_time_has_a_stable_deadline(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    db.execute('DELETE FROM subtitle_studio_media')
    rows = jobs(scheduler)
    first = plan(db, rows, now=50000)['items'][0]
    rows[0]['updated'] = 90000
    second = plan(db, rows, now=90000)['items'][0]
    assert second['deadline_at'] == first['deadline_at']
    assert first['status'] == second['status'] == 'overdue'


def test_newly_arrived_manifest_starts_preparation_clock(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    db.execute('DELETE FROM subtitle_studio_media')
    rows = jobs(scheduler)
    db.execute("""INSERT OR REPLACE INTO episodes(show_id,episode,hash,status,first_completed)
                  VALUES(42,1,'42-1','complete',49900)""")
    first = plan(db, rows, now=50000)['items'][0]
    assert first['deadline_at'] == 49900 + DEADLINE
    assert first['status'] == 'actionable'
    db.execute('INSERT INTO subtitle_studio_media VALUES(42,1,60000)')
    assert plan(db, rows, now=75000)['items'][0]['deadline_at'] == first['deadline_at']


def test_blocker_order_and_duplicates_do_not_reset_a_condition(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    rows[0].update(dispatch_state='blocked-stopped', thread_id='original-owner')
    rows[0]['payload'].update(owner_stopped_writing=True, recovery_class='system', recovery_turn='turn-1',
                              production_blockers=['font failed', 'player failed'])
    first = plan(db, rows)['items'][0]['condition']
    rows[0]['payload']['production_blockers'] = ['player failed', 'font failed', 'font failed']
    assert plan(db, rows)['items'][0]['condition'] == first


def test_evidence_content_dedupes_replay_but_cooldown_rejects_new_failure(delivery, tmp_path):
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    rows = jobs(scheduler)
    first = plan(db, rows)['next_action']
    root = db.path.parent / 'subtitle-studio'
    root.mkdir(parents=True, exist_ok=True)
    one, two = root / 'one.json', root / 'copy.json'
    one.write_text('{"exit_code":1}')
    two.write_text('{"exit_code":1}')
    recorded = record_attempt(db, plan(db, rows), first['job_id'], first['action'], 'failed', [one], now=2000)
    replay = record_attempt(db, plan(db, rows, 2001), first['job_id'], first['action'], 'failed', [two], now=2001)
    assert replay['checked_at'] == recorded['checked_at'] == 2000
    assert replay['evidence'][0]['snapshot'] == recorded['evidence'][0]['snapshot']
    assert (db.path.parent / 'subtitle-studio/recovery/evidence').joinpath(
        recorded['evidence'][0]['sha256'] + '.json').read_text() == '{"exit_code":1}'
    one.write_text('{"exit_code":2}')
    second = plan(db, rows, 2001)['items'][0]
    record_attempt(db, plan(db, rows, 2001), second['job_id'], second['action'], 'failed', [one], now=2001)
    cooling = plan(db, rows, 2002)['items'][0]
    assert cooling['status'] == 'retry_wait'
    one.write_text('{"exit_code":3}')
    with pytest.raises(ValueError, match='cooling down'):
        record_attempt(db, plan(db, rows, 2002), cooling['job_id'], cooling['action'], 'failed', [one], now=2002)


def test_monitor_detects_completed_chat_without_real_recovery_progress(delivery, tmp_path, monkeypatch):
    import json
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    db.execute("UPDATE subtitle_delivery_jobs SET state='preparing' WHERE id='episode:42:1'")
    now = [1000.0]
    monkeypatch.setattr('anime.subtitle_recovery.time.time', lambda: now[0])
    first = update_monitor(db.path)
    assert first['execution']['status'] == 'pending' and first['execution']['job_id'] == 'episode:42:1'
    monkeypatch.setattr('anime.subtitle_chinese.chinese_preparation', lambda *a, **kw: {
        'action': 'find_chinese', 'actions': ['find_chinese'], 'revision': 'route-name-changed-without-work', 'candidates': []})
    now[0] += EXECUTION_STALL
    second = update_monitor(db.path)
    assert second['execution']['status'] == 'stalled'
    assert second['execution']['stalled_since'] == first['checked_at']
    written = json.loads((db.path.parent / 'diagnostics/subtitle-recovery/status.json').read_text(encoding='utf-8'))
    assert written['execution'] == second['execution']


def test_monitor_failure_replaces_stale_actionable_status(delivery, tmp_path, monkeypatch):
    import json
    from anime.subtitle_delivery import SubtitleDelivery
    db, scheduler, _, _ = delivery
    media(db, tmp_path)
    db.execute("UPDATE subtitle_delivery_jobs SET state='preparing' WHERE id='episode:42:1'")
    assert update_monitor(db.path)['error'] is None
    monkeypatch.setattr(SubtitleDelivery, 'snapshot', lambda self: (_ for _ in ()).throw(RuntimeError('synthetic monitor failure')))
    result = update_monitor(db.path)
    assert 'synthetic monitor failure' in result['error'] and result['execution']['status'] == 'error'
    written = json.loads((db.path.parent / 'diagnostics/subtitle-recovery/status.json').read_text(encoding='utf-8'))
    assert written['next_action'] is None and written['error'] == result['error']
