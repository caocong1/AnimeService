"""Clean download waiting is a projection of original receipts, never acquisition."""
import copy
import json

import pytest

from test_subtitle_delivery import delivery
from test_subtitle_preparation import jobs, media
from anime.read_projection import read_projection
from anime.subtitle_preparation import preparation_plan
from anime.subtitle_recovery import COOLDOWN, DEADLINE, record_attempt, recovery_plan


@pytest.fixture
def clean_wait(delivery, tmp_path):
    db, scheduler, *_ = delivery
    db.set('subtitle_studio_enabled', True)
    db.set('subtitle_clean_media_preferred', True)
    mapping = db.mapping(42)
    mapping['aliases'] = ['Example']
    db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    media(db, tmp_path, 42, 2)  # An older cooked release is already local.
    row = next(r for r in jobs(scheduler) if r['id'] == 'episode:42:2')
    row['payload']['assessment'].update(clean_review_ready=False, capability_ready=True,
                                       reasons=['needs_actual_clean_mother_review'])
    db.execute('UPDATE subtitle_delivery_jobs SET state=?,payload=? WHERE id=?',
               ('preparing', json.dumps(row['payload']), row['id']))
    source = db.execute("INSERT INTO sources(show_id,kind,url) VALUES(42,'mikan','https://example.invalid/feed')")
    title = '[Group] Example S01E02 [1080p] [SoftSubs]'
    h = 'a' * 40
    cid = db.execute('''INSERT INTO candidates(show_id,source_id,title,url,source_metadata)
        VALUES(42,?,?,?,?)''', (source, title, f'https://example.invalid/{h}.torrent',
                               json.dumps({'expected_hash': h, 'declaration_only': True})))
    selected = {'index': 0, 'name': '[Group] Example S01E02 [1080p].mkv', 'size': 100}
    selection = {'files': [selected], 'ids': [0], 'selected': {'2': selected}}
    root = tmp_path / 'Anime' / 'Example [bgm-42]' / 'Season 01' / 'Subtitle samples' / h
    db.execute('''INSERT INTO tasks(hash,show_id,title,status,selection,save_path,acknowledged,provider,purpose)
        VALUES(?,42,?,'downloading',?,?,1,'qbit','subtitle_sample')''',
        (h, title, json.dumps(selection), str(root)))
    db.execute('''CREATE TABLE subtitle_samples(candidate_id INTEGER PRIMARY KEY,show_id INTEGER,episode INTEGER,
        family TEXT,hash TEXT,state TEXT,path TEXT DEFAULT '',size INTEGER,error TEXT DEFAULT '',updated REAL,evidence TEXT)''')
    db.execute("INSERT INTO subtitle_samples VALUES(?,42,2,'original-family',?,'queued','',100,'',1800,?)",
               (cid, h, json.dumps({'workflow_role': 'clean_base'})))
    return db, scheduler, row, h, cid, source, root / selected['name']


def plan(fixture, now=50000):
    db, _, row, *_ = fixture
    with read_projection(db.path) as view:
        return recovery_plan(view, [copy.deepcopy(row)], preparation_plan(view, [row], now), now)


def item(fixture, now=50000):
    return plan(fixture, now)['items'][0]


def facts(db):
    return {r['name']: db.rows('SELECT * FROM ' + r['name'] + ' ORDER BY rowid')
            for r in db.rows("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


def test_confirmed_download_waits_without_local_clean_file_or_business_writes(clean_wait):
    db, _, row, h, cid, _, file = clean_wait
    before = facts(db)
    result = plan(clean_wait)
    waiting = result['items'][0]
    assert waiting['status'] == 'waiting_media' and waiting['reason'] == 'clean_base_download'
    assert waiting['action'] is None and result['next_action'] is None
    assert waiting['media_task'] == {'candidate_id': cid, 'hash': h, 'acknowledged': 1, 'status': 'downloading'}
    assert waiting['deadline_at'] == 1500 + DEADLINE and waiting['input_key'] == row['input_key']
    assert '下载完成' in waiting['explanation'] and not file.exists()
    assert result['summary']['waiting_media'] == 1
    assert facts(db) == before


@pytest.mark.parametrize('field,value', [
    ('acknowledged', 0), ('status', 'intent'), ('status', 'uncertain'), ('status', 'unknown'),
    ('status', 'error'), ('status', 'held'), ('status', 'complete'), ('status', 'linked'),
    ('status', 'cleaned'), ('purpose', 'episode'), ('provider', 'other'), ('show_id', 43),
    ('error', 'original downloader error'),
])
def test_only_original_acknowledged_downloading_task_is_normal_wait(clean_wait, field, value):
    db, _, _, h, *_ = clean_wait
    db.execute(f'UPDATE tasks SET {field}=? WHERE hash=?', (value, h))
    row = item(clean_wait)
    assert row['status'] != 'waiting_media' and 'media_task' not in row
    assert row['action'] == 'bind_clean_candidate'
    assert row['explanation'] == '核验洁净母片来源，完成后经受控入口绑定'


@pytest.mark.parametrize('state,lease,committing,error', [
    ('unknown', '', 0, ''), ('pending', '', 0, ''), ('intent', '', 0, ''),
    ('done', '', 1, ''), ('done', 'original-lease', 0, ''), ('done', '', 0, 'receipt error'),
])
def test_queue_receipt_hold_is_not_normal_wait(clean_wait, state, lease, committing, error):
    db, _, _, h, *_ = clean_wait
    db.execute('INSERT INTO thunder_queue(hash,state,lease,committing,error) VALUES(?,?,?,?,?)',
               (h, state, lease, committing, error))
    assert item(clean_wait)['status'] != 'waiting_media'


def test_unknown_dialog_is_not_normal_wait(clean_wait):
    db, _, _, h, *_ = clean_wait
    db.set('thunder_dialog_hold_' + h, {'unconfirmed': True})
    assert item(clean_wait)['status'] != 'waiting_media'


@pytest.mark.parametrize('change', ['reference', 'missing_role', 'sample_error', 'sample_state',
    'disabled_source', 'foreign_source', 'foreign_candidate', 'candidate_hash', 'expected_hash',
    'declaration_conflict', 'hard_title', 'changed_title', 'wrong_episode', 'wrong_season', 'duplicate_owner'])
def test_source_identity_role_and_protections_are_required(clean_wait, change):
    db, _, _, h, cid, source, *_ = clean_wait
    if change in ('reference', 'missing_role'):
        evidence = {'workflow_role': 'subtitle_reference'} if change == 'reference' else {}
        db.execute('UPDATE subtitle_samples SET evidence=?', (json.dumps(evidence),))
    elif change == 'sample_error':
        db.execute("UPDATE subtitle_samples SET error='original sample error'")
    elif change == 'sample_state':
        db.execute("UPDATE subtitle_samples SET state='error'")
    elif change in ('disabled_source', 'foreign_source'):
        db.execute('UPDATE sources SET ' + ('enabled=0' if change == 'disabled_source' else 'show_id=43') + ' WHERE id=?', (source,))
    elif change == 'foreign_candidate':
        db.execute('UPDATE candidates SET show_id=43 WHERE id=?', (cid,))
    elif change == 'candidate_hash':
        db.execute('UPDATE candidates SET hash=? WHERE id=?', ('b' * 40, cid))
    elif change in ('expected_hash', 'declaration_conflict'):
        metadata = {'expected_hash': 'b' * 40} if change == 'expected_hash' else {'acquisition_declaration_conflict': True}
        db.execute('UPDATE candidates SET source_metadata=? WHERE id=?', (json.dumps(metadata), cid))
    elif change in ('hard_title', 'changed_title', 'wrong_episode', 'wrong_season'):
        title = db.one('SELECT title FROM candidates WHERE id=?', (cid,))['title']
        title = (title.replace('SoftSubs', 'HardSubs') if change == 'hard_title' else
                 title.replace('E02', 'E01') if change == 'wrong_episode' else
                 title.replace('S01', 'S02') if change == 'wrong_season' else title + ' updated')
        db.execute('UPDATE candidates SET title=? WHERE id=?', (title, cid))
        if change != 'changed_title':
            db.execute('UPDATE tasks SET title=? WHERE hash=?', (title, h))
    else:
        db.execute("INSERT INTO subtitle_samples VALUES(9999,43,2,'other',?,'queued','',100,'',1800,?)",
                   (h, json.dumps({'workflow_role': 'clean_base'})))
    assert item(clean_wait)['status'] != 'waiting_media'


@pytest.mark.parametrize('change', ['other_episode', 'wrong_ids', 'wrong_size', 'escape', 'wrong_file', 'two_in_flight'])
def test_exact_single_episode_manifest_and_unique_in_flight_task(clean_wait, change):
    db, _, _, h, cid, source, *_ = clean_wait
    task = db.one('SELECT * FROM tasks WHERE hash=?', (h,))
    selection = json.loads(task['selection'])
    if change == 'two_in_flight':
        other = 'b' * 40
        second = db.execute('INSERT INTO candidates(show_id,source_id,title,url) VALUES(42,?,?,?)',
                            (source, task['title'], f'https://example.invalid/{other}.torrent'))
        db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,acknowledged,purpose) VALUES(?,42,?,'intent',?,?,0,'subtitle_sample')",
                   (other, task['title'], task['selection'], task['save_path']))
        db.execute("INSERT INTO subtitle_samples VALUES(?,42,2,'second',?,'queued','',100,'',1800,?)",
                   (second, other, json.dumps({'workflow_role': 'clean_base'})))
    else:
        if change == 'other_episode':
            selection['selected'] = {'1': selection['selected']['2']}
        elif change == 'wrong_ids':
            selection['ids'] = []
        elif change == 'wrong_size':
            selection['selected']['2']['size'] += 1
        else:
            name = '../../' + selection['files'][0]['name'] if change == 'escape' else '[Group] Other S01E02.mkv'
            selection['files'][0]['name'] = selection['selected']['2']['name'] = name
        db.execute('UPDATE tasks SET selection=? WHERE hash=?', (json.dumps(selection), h))
    assert item(clean_wait)['status'] != 'waiting_media'


@pytest.mark.parametrize('patch', [
    {'dispatch_state': 'unknown'}, {'dispatch_state': 'committing'}, {'dispatch_state': 'acknowledged'},
    {'dispatch_state': 'blocked-stopped'}, {'thread_id': 'original-owner'}, {'lease_owner': 'dispatcher'},
    {'lease_expires': 99999}, {'scope_blocker': 'outside scope'},
])
def test_job_protections_precede_download_wait(clean_wait, patch):
    clean_wait[2].update(patch)
    row = item(clean_wait)
    assert row['status'] != 'waiting_media' and row['action'] is None


@pytest.mark.parametrize('change', ['watched', 'unauthorized', 'bounds', 'cleanup', 'policy', 'live_owner',
    'live_lease', 'stopped', 'frozen', 'studio_writer', 'viewing_lease'])
def test_live_protections_precede_wait_even_for_stale_job_snapshot(clean_wait, change):
    db, _, _, _, _, _, _ = clean_wait
    if change == 'watched':
        db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    elif change in ('unauthorized', 'cleanup'):
        db.execute('UPDATE shows SET ' + ('authorized=0' if change == 'unauthorized' else 'cleanup_hold=1') + ' WHERE id=42')
    elif change == 'bounds':
        mapping = db.mapping(42)
        mapping['end'] = 1
        db.execute('UPDATE shows SET mapping=? WHERE id=42', (json.dumps(mapping),))
    elif change == 'policy':
        db.execute("UPDATE source_policies SET config='{}' WHERE show_id=42")
    elif change in ('live_owner', 'live_lease'):
        column = 'thread_id' if change == 'live_owner' else 'lease_token'
        db.execute(f"UPDATE subtitle_delivery_jobs SET {column}='protected' WHERE id='episode:42:2'")
    elif change == 'stopped':
        payload = copy.deepcopy(clean_wait[2]['payload'])
        payload['user_stopped'] = True
        db.execute("UPDATE subtitle_delivery_jobs SET payload=? WHERE id='episode:42:2'", (json.dumps(payload),))
    elif change == 'studio_writer':
        db.execute("UPDATE subtitle_studio_jobs SET state='production' WHERE show_id=42 AND episode=2")
    else:
        from anime.subtitle_targets import tables
        tables(db)
        if change == 'frozen':
            db.execute("INSERT INTO subtitle_targets VALUES('fixed',42,2,1,'{}',1)")
            db.execute("INSERT INTO subtitle_target_heads VALUES(42,2,'fixed')")
        else:
            db.execute("INSERT INTO subtitle_viewing_leases VALUES('active',42,2,'media',99999)")
    row = item(clean_wait)
    assert row['status'] == 'protected' and row['action'] is None and 'media_task' not in row


def test_download_wait_preserves_deadline_condition_failures_and_cooldown(clean_wait):
    db, _, job, h, *_ = clean_wait
    db.execute("UPDATE tasks SET status='intent',acknowledged=0 WHERE hash=?", (h,))
    before = plan(clean_wait, 2000)
    baseline = before['items'][0]
    evidence = db.path.parent / 'subtitle-studio' / 'original-clean-failure.json'
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text('{"result":"no completed clean base yet"}', encoding='utf-8')
    record_attempt(db, before, job['id'], baseline['action'], 'failed', [evidence], now=2000)
    db.execute("UPDATE tasks SET status='downloading',acknowledged=1 WHERE hash=?", (h,))
    waiting = item(clean_wait, 2001)
    assert waiting['status'] == 'waiting_media'
    assert waiting['condition'] == baseline['condition']
    assert waiting['failed_attempts'] == 1 and waiting['last_attempt_at'] == 2000
    assert waiting['deadline_at'] == baseline['deadline_at'] and waiting['next_check_at'] == 2000 + COOLDOWN
    # Read time/refresh cannot move the original six-hour reminder.
    job['updated'] = 90000
    assert item(clean_wait, 90000)['deadline_at'] == baseline['deadline_at']
    db.execute("UPDATE tasks SET status='complete' WHERE hash=?", (h,))
    completed = item(clean_wait, 2002)
    assert completed['status'] == 'retry_wait' and completed['failed_attempts'] == 1
    assert completed['next_check_at'] == 2000 + COOLDOWN and completed['condition'] == baseline['condition']


def test_download_wait_does_not_clear_exhausted_original_failure_history(clean_wait):
    db, _, job, h, *_ = clean_wait
    db.execute("UPDATE tasks SET status='intent',acknowledged=0 WHERE hash=?", (h,))
    evidence = db.path.parent / 'subtitle-studio' / 'original-clean-failures.json'
    evidence.parent.mkdir(exist_ok=True)
    for at in (2000, 2000 + COOLDOWN):
        evidence.write_text(json.dumps({'actual_failure_at': at}), encoding='utf-8')
        current = plan(clean_wait, at)
        record_attempt(db, current, job['id'], 'bind_clean_candidate', 'failed', [evidence], now=at)
    baseline = item(clean_wait, 2001 + COOLDOWN)
    assert baseline['status'] == 'repair_blocked' and baseline['failed_attempts'] == 2
    db.execute("UPDATE tasks SET status='downloading',acknowledged=1 WHERE hash=?", (h,))
    waiting = item(clean_wait, 2002 + COOLDOWN)
    assert waiting['status'] == 'waiting_media' and waiting['failed_attempts'] == 2
    assert waiting['deadline_at'] == baseline['deadline_at'] and waiting['condition'] == baseline['condition']
    db.execute("UPDATE tasks SET status='complete' WHERE hash=?", (h,))
    assert item(clean_wait, 2003 + COOLDOWN)['status'] == 'repair_blocked'


@pytest.mark.parametrize('dry_run', [True, False])
def test_waiting_never_allows_unfinished_task_to_bind_even_if_file_exists(clean_wait, dry_run):
    from anime.subtitle_evidence import bind_target
    db, scheduler, _, _, _, _, file = clean_wait
    file.parent.mkdir(parents=True)
    file.write_bytes(b'x' * 100)
    before = facts(db)
    with pytest.raises(ValueError, match='selected completed file'):
        bind_target(scheduler, 42, 2, file, dry_run=dry_run)
    assert facts(db) == before
