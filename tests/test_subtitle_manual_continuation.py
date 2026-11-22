"""Synthetic state-machine fixtures only; no real host, media, or downloads."""
import copy
import json
import sqlite3
import hashlib
import time

import pytest

from test_subtitle_delivery import delivery, begin
from anime.subtitle_manual_continuation import continue_manually, SCHEMA, TABLE
import anime.subtitle_manual_continuation as manual


def request(scheduler, now):
    identity, intent = begin(scheduler)
    now[0] += 901
    auth = {'schema': SCHEMA, 'job_id': identity, 'input_key': intent['input_key'],
        'expected_revision': intent['revision'], 'responsible_thread_id': 'human-requested-fixture',
        'explicit_human_approval': True, 'human_answer': 'Synthetic explicit approval for this episode only',
        'original_turn_id': 'original-fixture-turn', 'original_failed_command_id': 'original-fixture-command'}
    native = {'id': 'original-fixture-turn', 'status': 'completed', 'startedAt': 990,
        'completedAt': 1010, 'items': [{'id': 'original-fixture-command',
            'type': 'commandExecution', 'status': 'failed', 'exitCode': 1,
            'command': 'python scripts/subtitle_delivery_queue.py begin ' + identity,
            'output': {'text': 'save_ticket protect_ticket_directory cannot secure the ticket directory',
                       'truncated': False}}]}
    kwargs = {'identity': identity, 'thread_id': auth['responsible_thread_id'],
        'expected_revision': intent['revision'], 'expected_input': intent['input_key'],
        'authorization': (auth, {'path': 'synthetic-authorization.json', 'sha256': 'a'*64}),
        'native': (native, {'path': 'synthetic-native.json', 'sha256': 'b'*64})}
    return kwargs


def test_plan_is_read_only_and_apply_retains_original_then_replays(delivery):
    db, scheduler, ready, now = delivery
    args = request(scheduler, now)
    original = db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (args['identity'],))
    protected = {t: db.rows(f'SELECT * FROM {t}') for t in ('episodes', 'watches', 'tasks', 'dispatch_receipts')}
    assert continue_manually(scheduler, **args)['status'] == 'planned'
    assert not db.one("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,))
    assert db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (args['identity'],)) == original
    result = continue_manually(scheduler, **args, dry_run=False)
    current = db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (args['identity'],))
    assert current['thread_id'] == args['thread_id']
    assert current['dispatch_state'] == 'acknowledged'
    assert json.loads(db.one(f'SELECT original_row FROM {TABLE}')['original_row']) == original
    for k in ('input_key', 'lease_owner', 'lease_token', 'lease_expires', 'dispatch_token', 'accepted_build_id'):
        assert current[k] == original[k]
    for k,v in json.loads(original['payload']).items():
        assert json.loads(current['payload'])[k] == v
    assert not result['dispatch_created_claimed'] and not result['dispatch_absence_claimed']
    assert all(db.rows(f'SELECT * FROM {t}') == rows for t,rows in protected.items())
    assert continue_manually(scheduler, **args, dry_run=False) == {**result, 'duplicate': True}
    assert len(db.rows(f'SELECT * FROM {TABLE}')) == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(f'DELETE FROM {TABLE}')


@pytest.mark.parametrize('change', ['approval','input','revision','owner','active_lease','watched',
    'stopped','scope','native_timeout','native_truncated','host_called','wrong_turn','wrong_operation','episode_prefix','not_ready'])
def test_protected_cases_do_not_assign(delivery, change):
    db,scheduler,ready,now = delivery
    args=request(scheduler,now)
    auth=args['authorization'][0]; native=args['native'][0]
    if change=='approval': auth['explicit_human_approval']=False
    if change=='input': args['expected_input']='changed'
    if change=='revision': db.execute('UPDATE subtitle_delivery_jobs SET revision=revision+1 WHERE id=?',(args['identity'],))
    if change=='owner': db.execute("UPDATE subtitle_delivery_jobs SET thread_id='existing-fixture-owner' WHERE id=?",(args['identity'],))
    if change=='active_lease': now[0]=1001
    if change=='watched': db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    if change=='stopped':
        row=scheduler.get(args['identity']);p=row['payload'];p['user_stopped']=True
        db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?',(json.dumps(p),args['identity']))
    if change=='scope': db.execute("UPDATE shows SET state='hold' WHERE id=42")
    if change=='native_timeout': native['items'][0]['exitCode']=124
    if change=='native_truncated': native['items'][0]['output']['truncated']=True
    if change=='host_called': native['items'].append({'type':'mcpToolCall','tool':'create_thread'})
    if change=='wrong_turn': native['startedAt']=1005
    if change=='wrong_operation': native['items'][0]['command']='python begin episode:43:1'
    if change=='episode_prefix': native['items'][0]['command'] += '0'
    if change=='not_ready': ready.values[(42,1)]={**ready(db,42,1,{}),'material_ready':False}
    before=db.rows('SELECT * FROM subtitle_delivery_jobs')
    events=db.rows('SELECT * FROM subtitle_delivery_events')
    with pytest.raises(ValueError): continue_manually(scheduler,**args,dry_run=False)
    assert db.rows('SELECT * FROM subtitle_delivery_jobs')==before
    assert db.rows('SELECT * FROM subtitle_delivery_events')==events
    assert not db.one("SELECT 1 FROM sqlite_master WHERE name=?",(TABLE,))


def test_current_chat_cannot_own_two_episodes(delivery):
    db,scheduler,ready,now=delivery
    args=request(scheduler,now)
    # Current unconfirmed intent already occupies capacity; increase bounded
    # capacity through the real policy before making the other synthetic owner.
    scheduler.set_production_limit(3)
    other, intent=begin(scheduler,43,1)
    scheduler.reconcile_dispatch(other,intent['dispatch_token'],thread_id=args['thread_id'])
    with pytest.raises(ValueError,match='thread_owner_active'):
        continue_manually(scheduler,**args,dry_run=False)


@pytest.mark.parametrize('mutate_original', [False, True])
def test_only_reconstructed_append_only_archives_keep_fixed_input(delivery, mutate_original):
    import hashlib
    db,scheduler,ready,now=delivery
    args=request(scheduler,now)
    def signature(drafts):
        return hashlib.sha256(json.dumps({'target':'synthetic-media-42-1','drafts':drafts},
            sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    original=[{'id':'original-fixture','raw_hash':'a'*64,'text_hash':'baseline-text'}]
    media={'kind':'completed_media','hash':'fixed-fixture','target':{'fingerprint':'synthetic-media-42-1'}}
    base=ready(db,42,1,{})
    old={**base,'material_refs':[media,{'kind':'archived_draft',**original[0]}],
         'input_signature':signature(original)}
    current=copy.deepcopy(original)+[{'id':'new-fixture','raw_hash':'b'*64,'text_hash':'new-text'}]
    if mutate_original:current[0]['raw_hash']='c'*64
    ready.values[(42,1)]={**base,'material_refs':[media,*[{'kind':'archived_draft',**d} for d in current]],
        'input_signature':signature(current),'text_hashes':['baseline-text','new-text']}
    job=scheduler.get(args['identity']);job['payload']['assessment']=old
    db.execute('UPDATE subtitle_delivery_jobs SET input_key=?,payload=? WHERE id=?',
        (old['input_signature'],json.dumps(job['payload']),args['identity']))
    args['expected_input']=old['input_signature'];args['authorization'][0]['input_key']=old['input_signature']
    if mutate_original:
        with pytest.raises(ValueError,match='current verified ready inputs'):
            continue_manually(scheduler,**args,dry_run=False)
    else:
        assert continue_manually(scheduler,**args,dry_run=False)['status']=='assigned'
        assert scheduler.get(args['identity'])['input_key']==old['input_signature']


def timeout_request(delivery, monkeypatch):
    db, scheduler, ready, now = delivery
    args = request(scheduler, now)
    auth = args['authorization'][0]
    auth.update(schema=manual.TIMEOUT_SCHEMA, preserve_original_unknown_intent=True,
                production_dispatch_messages_reviewed_absent=True)
    folder = db.path.parent.parent/'data/subtitle-studio/synthetic-only'
    folder.mkdir(parents=True)
    wrapper = folder/'begin_once.py'
    wrapper.write_text("subprocess.run([str(R/'scripts/subtitle_delivery_queue.py'),'begin','"+args['identity']+"'],timeout=120)",encoding='utf-8')
    auth['original_wrapper'] = {'path': str(wrapper), 'sha256': hashlib.sha256(wrapper.read_bytes()).hexdigest()}
    maintenance = {'type': 'mcpToolCall', 'id': 'synthetic-maintenance', 'tool': 'send_message_to_thread',
                   'arguments': {'threadId':'engineering-fixture','prompt':'Inspect engineering only; do not dispatch.'}}
    auth['allowed_maintenance_messages'] = [{'id':'synthetic-maintenance','thread_id':'engineering-fixture',
        'prompt_sha256':hashlib.sha256(maintenance['arguments']['prompt'].encode()).hexdigest(),
        'engineering_only_no_production_dispatch':True}]
    native = {'thread_id':'original-fixture-thread','turn_row':{'thread_id':'original-fixture-thread',
        'turn_id':auth['original_turn_id'],'status':'completed','started_at':990,'completed_at':1010},
        'items':[{'id':auth['original_failed_command_id'],'type':'commandExecution','status':'failed',
            'exitCode':1,'command':'python synthetic-only/begin_once.py','output':None,
            'aggregatedOutput':"subprocess.TimeoutExpired: Command ['begin', '"+args['identity']+"'] timed out after 120 seconds"},maintenance]}
    args['native']=(native,{'path':'synthetic-native.json','sha256':'c'*64})
    args['preserve_timeout_intent']=True
    job=scheduler.get(args['identity']);job['payload']['assessment']['material_refs']=[{'kind':'completed_media','target':{'path':'synthetic-fixed-media.mkv'}}]
    db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?',(json.dumps(job['payload']),args['identity']))
    monkeypatch.setattr(manual,'_observe_related_processes',lambda *a:{'status':'observed','observed_at':time.time(),'related_processes':[]})
    return args


def test_explicit_timeout_route_retains_unknown_intent_and_current_observation(delivery, monkeypatch):
    db,scheduler,ready,now=delivery
    args=timeout_request(delivery,monkeypatch)
    original=db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?',(args['identity'],))
    assert continue_manually(scheduler,**args)['state_changed'] is False
    assert db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?',(args['identity'],))==original
    result=continue_manually(scheduler,**args,dry_run=False)
    assert result['status']=='assigned'
    archived=json.loads(db.one(f'SELECT original_row FROM {TABLE}')['original_row'])
    assert archived==original
    value=scheduler.get(args['identity'])['payload']['manual_continuation']
    assert value['original_outcome']=='unknown_preserved'
    assert value['current_process_observation']['related_processes']==[]
    assert not result['dispatch_created_claimed'] and not result['dispatch_absence_claimed']
    assert continue_manually(scheduler,**args,dry_run=False)=={**result,'duplicate':True}


@pytest.mark.parametrize('change',['default_route','unknown_ack','approval','wrapper_changed',
    'wrong_job','extra_dispatch','changed_maintenance','unreviewed_maintenance','related_writer',
    'stale_process','failed_observation','owner','watched','lease','input','stopped'])
def test_timeout_exception_does_not_relax_other_protections(delivery,monkeypatch,change):
    db,scheduler,ready,now=delivery
    args=timeout_request(delivery,monkeypatch);auth=args['authorization'][0];native=args['native'][0]
    if change=='default_route':args['preserve_timeout_intent']=False
    if change=='unknown_ack':auth['preserve_original_unknown_intent']=False
    if change=='approval':auth['explicit_human_approval']=False
    if change=='wrapper_changed':auth['original_wrapper']['sha256']='0'*64
    if change=='wrong_job':native['items'][0]['aggregatedOutput']=native['items'][0]['aggregatedOutput'].replace(args['identity'],args['identity']+'0')
    if change=='extra_dispatch':native['items'].append({'type':'mcpToolCall','tool':'create_thread'})
    if change=='changed_maintenance':native['items'][1]['arguments']['prompt']='Changed work'
    if change=='unreviewed_maintenance':auth['allowed_maintenance_messages']=[]
    if change in ('related_writer','stale_process','failed_observation'):
        monkeypatch.setattr(manual,'_observe_related_processes',lambda *a:{'status':'error' if change=='failed_observation' else 'observed',
            'observed_at':time.time()-(60 if change=='stale_process' else 0),'related_processes':[{'pid':123}] if change=='related_writer' else []})
    if change=='owner':db.execute("UPDATE subtitle_delivery_jobs SET thread_id='existing-owner' WHERE id=?",(args['identity'],))
    if change=='watched':db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    if change=='lease':now[0]=1001
    if change=='input':args['expected_input']='different'
    if change=='stopped':
        job=scheduler.get(args['identity']);job['payload']['user_stopped']=True
        db.execute('UPDATE subtitle_delivery_jobs SET payload=? WHERE id=?',(json.dumps(job['payload']),args['identity']))
    before=db.rows('SELECT * FROM subtitle_delivery_jobs');events=db.rows('SELECT * FROM subtitle_delivery_events')
    with pytest.raises(ValueError):continue_manually(scheduler,**args,dry_run=False)
    assert db.rows('SELECT * FROM subtitle_delivery_jobs')==before
    assert db.rows('SELECT * FROM subtitle_delivery_events')==events


@pytest.mark.parametrize('fault',[None,'expired','wrong_input','wrong_target','changed_file'])
def test_fresh_capability_is_real_bound_and_atomically_registered(delivery,monkeypatch,fault):
    db,scheduler,ready,now=delivery
    args=timeout_request(delivery,monkeypatch)
    original=db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?',(args['identity'],))
    key='subtitle_delivery_readiness_42_1'
    old={'target':{'fingerprint':'synthetic-media-42-1'},'delivery_capability_report':{'path':'old-fixture','sha256':'d'*64}}
    db.set(key,old)
    def live(store,sid,ep,payload):
        base=ready(store,sid,ep,payload)
        ok=store.get(key,{}).get('delivery_capability_report')!=old['delivery_capability_report']
        return {**base,'capability_ready':ok,'reasons':[] if ok else ['needs_verified_local_finished_output_tools']}
    scheduler.readiness=live
    with pytest.raises(ValueError,match='current verified ready inputs'):
        continue_manually(scheduler,**args)
    report={'role':'finished_v1_technical_preparation','show_id':42,'episode':1,
        'job_id':args['identity'],'input_key':args['expected_input'],
        'target_fingerprint':'synthetic-media-42-1','reviewer':'Synthetic tool fixture',
        'technical_decode_passed':True,'output_tools_verified':True,'local_viewer_available':True,
        'expires_at':time.time()+600,'evidence':[{'path':'synthetic-output-only','sha256':'e'*64}]}
    if fault=='expired':report['expires_at']=time.time()-1
    if fault=='wrong_input':report['input_key']='different'
    if fault=='wrong_target':report['target_fingerprint']='different'
    file=db.path.parent/'subtitle-studio/synthetic-only/fresh.json'
    file.parent.mkdir(parents=True,exist_ok=True);file.write_text(json.dumps(report))
    reference={'path':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    args['capability_evidence']=(report,reference)
    if fault=='changed_file':file.write_text('{}')
    if fault:
        with pytest.raises(ValueError):continue_manually(scheduler,**args,dry_run=False)
        assert db.get(key)==old
        assert db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?',(args['identity'],))==original
    else:
        assert continue_manually(scheduler,**args)['status']=='planned'
        assert db.get(key)==old
        continue_manually(scheduler,**args,dry_run=False)
        assert db.get(key)=={**old,'delivery_capability_report':reference}
        stored=json.loads(db.one(f'SELECT original_row FROM {TABLE}')['original_row'])
        assert stored==original
        assert scheduler.get(args['identity'])['payload']['assessment']==json.loads(original['payload'])['assessment']
        assert continue_manually(scheduler,**args,dry_run=False)['duplicate'] is True
