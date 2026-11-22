"""A coordinator must account for actual work, rather than recopying an idle closure."""
from copy import deepcopy

import pytest

from anime.subtitle_recovery import round_status


def plan(status='overdue', action='find_chinese', **extra):
    row = {'job_id': 'episode:42:1', 'input_key': 'fixed-input', 'condition': 'fixed-condition',
           'status': status, 'action': action, **extra}
    return {'checked_at': 1000, 'items': [row]}


def test_fresh_observations_and_changed_words_do_not_close_due_work():
    before = plan()
    after = plan(explanation='new wording')
    after['checked_at'] = 1100
    for value in (after, plan(condition='changed-condition'), plan(action='collect_chinese_source')):
        result = round_status(before, value, [], now=1100)
        assert not result['complete'] and result['status'] == 'unhandled_actionable'
    progress = {**before['items'][0], 'checked_at': 1050, 'outcome': 'progress'}
    assert not round_status(before, after, [progress], now=1100)['complete']


def test_real_receipt_must_match_the_round_input_action_and_time():
    before = plan()
    receipt = {**before['items'][0], 'checked_at': 1050, 'outcome': 'failed'}
    assert round_status(before, plan(), [receipt], now=1100)['recorded_attempts'] == 1
    for change in ({'checked_at': 999}, {'checked_at': 1200}, {'input_key': 'old'},
                   {'condition': 'other'}, {'action': 'verify_output'}):
        assert not round_status(before, plan(), [{**receipt, **change}], now=1100)['complete']
    assert round_status(before, plan('retry_wait'), [], now=1100)['status'] == 'waiting'


def test_one_real_bounded_action_can_finish_a_round_and_exposes_remaining_work():
    before = plan()
    second = {**before['items'][0], 'job_id': 'episode:43:1'}
    before['items'].append(second)
    after = deepcopy(before)
    after['items'][0]['status'] = 'source_wait'
    result = round_status(before, after, [], now=1100)
    assert result['complete'] and result['advanced_jobs'] == ['episode:42:1']
    assert result['pending'][0]['job_id'] == 'episode:43:1'


def test_other_work_cannot_cover_idle_dispatch_slots():
    job={'job_id':'episode:43:1','input_key':'stable'}
    before=plan()
    before['dispatch']={'ready':[job],'capacity':{'available':1}}
    after=plan('source_wait')
    after['dispatch']=before['dispatch']
    result=round_status(before,after,[],now=1100)
    assert not result['complete'] and result['status']=='dispatch_slots_unfilled'
    after['dispatch']={'ready':[],'capacity':{'available':0},'acknowledged':[dict(job,thread_id='owner')]}
    result=round_status(before,after,[],now=1100)
    assert result['complete'] and result['confirmed_jobs']==[job['job_id']]


def test_independent_routes_official_dates_and_available_dispatch_slots_count_as_work():
    before = plan('source_wait')
    before['items'][0]['independent_routes'] = [plan('actionable', 'verify_output')['items'][0]]
    assert not round_status(before, before, [], now=1100)['complete']
    official = {'checked_at': 1000, 'items': [], 'official_release_review': {
        'items': [{'show_id': 42, 'episode': None, 'status': 'actionable',
                   'condition': 'mapping-sha', 'action': 'revalidate_official_schedule'}]}}
    assert round_status(official, official, [], now=1100)['pending'][0]['action'] == 'revalidate_official_schedule'
    ready = plan('ready', None)
    assert round_status(ready, ready, [], now=1100, capacity={'available': 0})['complete']
    assert not round_status(ready, ready, [], now=1100, capacity={'available': 1})['complete']


@pytest.mark.parametrize('bad', [None, [], {'items': []}, {'checked_at': True}, {'checked_at': 1200},
                               {'checked_at': -5000}, {'checked_at': 1000, 'error': 'failed'},
                               {'checked_at': 1000, 'items': ['bad']},
                               {'checked_at': 1000, 'official_release_review': {'error': 'failed'}}])
def test_missing_stale_or_broken_round_evidence_never_passes(bad):
    result = round_status(bad, plan(), [], now=1100)
    assert not result['complete'] and result['status'] == 'invalid_or_failed_snapshot'


@pytest.mark.parametrize('bad', [None, {}, {'checked_at': 1000}, {'checked_at': 1000, 'items': None},
                               {'checked_at': 1000, 'items': [], 'error': 'failed'}])
def test_broken_postcondition_cannot_turn_actionable_work_into_waiting(bad):
    assert round_status(plan(), bad, [], now=1100)['status'] == 'invalid_or_failed_snapshot'


def test_older_postcondition_cannot_close_due_work_or_hide_an_independent_route():
    before = plan()
    before['checked_at'] = 1100
    for after in (plan('retry_wait'), {'checked_at': 1000, 'items': []}):
        result = round_status(before, after, [], now=1200)
        assert not result['complete']
        assert result['status'] == 'invalid_or_failed_snapshot'


def test_newly_ready_episode_needs_dispatch_when_a_slot_is_free():
    before = plan()
    after = plan(status='ready', action=None)
    after.update(checked_at=1100, dispatch={'ready':[{'job_id':'episode:42:1','input_key':'fixed-input'}],
                                           'capacity':{'available':1}})
    result = round_status(before, after, [], now=1100)
    assert not result['complete'] and result['status']=='dispatch_slots_unfilled'
