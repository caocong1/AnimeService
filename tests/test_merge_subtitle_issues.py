import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_subtitle_delivery import delivery  # noqa: F401

from scripts.merge_subtitle_issues import merge_issues


def batch(root, name):
    evidence = root / (name + '.txt')
    evidence.write_text('Actual source check ' + name, encoding='utf-8')
    issue = {'id': name, 'show_id': 42, 'episode': 2, 'season': 1, 'status': 'archived_open',
             'blocks_delivery': False, 'category': 'op', 'title': name, 'dimensions': {'lyrics': 'unverified'},
             'current_release_decision': 'Keep verified Chinese', 'existing_attempts': ['Actual search'],
             'retry_requires': ['New matched lyrics'],
             'evidence_refs': [{'path': str(evidence), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}]}
    path = root / (name + '.json')
    path.write_text(json.dumps({'role': 'episode_pending_issue_merge', 'job': 'episode:42:2',
                               'owner': 'owner', 'input': 'fixed', 'issues': [issue]}), encoding='utf-8')
    return path


@pytest.fixture
def archive(tmp_path):
    folder = tmp_path / 'unresolved-issues'
    folder.mkdir()
    index = folder / 'issues.json'
    index.write_text('{"issues":[],"history":{"keep":"all"}}', encoding='utf-8')
    return tmp_path, index


def merge(root, path, **kwargs):
    return merge_issues(root, path, job='episode:42:2', owner='owner', expected_input='fixed', **kwargs)


def test_dry_run_idempotence_and_conflict_preserve_all_existing_data(archive):
    root, index = archive
    path = batch(root, 'first')
    before = index.read_bytes()
    assert merge(root, path)['added'] == ['first'] and index.read_bytes() == before
    assert merge(root, path, apply=True)['added'] == ['first']
    after = index.read_bytes()
    assert merge(root, path, apply=True)['already_archived'] == ['first'] and index.read_bytes() == after
    assert json.loads(after)['history'] == {'keep': 'all'}
    value = json.loads(path.read_text()); value['issues'][0]['title'] = 'Conflicting overwrite'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='overwrite'):
        merge(root, path, apply=True)
    assert index.read_bytes() == after
    assert next((index.parent / 'merge-backups').glob('*.json')).read_bytes() == before


def test_concurrent_appenders_preserve_both_episodes_and_history(archive):
    root, index = archive
    paths = [batch(root, name) for name in ('first', 'second')]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda path: merge(root, path, apply=True), paths))
    assert {row['id'] for row in json.loads(index.read_text())['issues']} == {'first', 'second'}
    assert all(result['applied'] for result in results)


def test_changed_evidence_and_wrong_owner_do_not_change_archive(archive):
    root, index = archive
    path = batch(root, 'first')
    before = index.read_bytes()
    with pytest.raises(ValueError, match='bind'):
        merge_issues(root, path, job='episode:42:2', owner='other', expected_input='fixed', apply=True)
    (root / 'first.txt').write_text('Changed source')
    with pytest.raises(ValueError, match='evidence'):
        merge(root, path, apply=True)
    assert index.read_bytes() == before

def test_v1_build_merges_only_current_owners_bound_batch(delivery, tmp_path):
    import sqlite3
    from scripts.merge_subtitle_issues import merge_for_build
    from test_subtitle_delivery import begin
    db, service, _, _ = delivery
    job, intent = begin(service, 42, 2)
    service.reconcile_dispatch(job, intent['dispatch_token'], thread_id='fixture-owner')
    project = db.path.parent.parent
    with sqlite3.connect(db.path) as source, sqlite3.connect(project/'data/anime.db') as target:
        source.backup(target)
    root = project/'data/subtitle-studio'
    folder = root/'current-episode'
    (folder/'v1').mkdir(parents=True)
    index = root/'unresolved-issues/issues.json'
    index.parent.mkdir()
    index.write_text('{"issues":[]}', encoding='utf8')
    temporary = batch(folder, 'scene-fact')
    data = json.loads(temporary.read_text('utf8'))
    data.update(job=job, input=intent['input_key'], owner='fixture-owner')
    (folder/'issues-pending.json').write_text(json.dumps(data), encoding='utf8')
    config = {'delivery_job_id':job, 'delivery_thread_id':'other', 'refinement_review':str(folder/'v1/review.json'),
              'show_id':42,'episode':2}
    with pytest.raises(ValueError, match='owner'):
        merge_for_build(project, config)
    assert json.loads(index.read_text())['issues'] == []
    config['delivery_thread_id'] = 'fixture-owner'
    assert merge_for_build(project, config)['added'] == ['scene-fact']
    assert not merge_for_build(project, config)['added']
