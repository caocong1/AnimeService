import copy
import json
from types import SimpleNamespace

import pytest

from anime.dispatch_caller import DispatchFailure, dispatch_once, verified_ticket
from test_subtitle_delivery import delivery, begin


def archive(tmp_path, intent):
    ticket = intent['ticket']
    path = tmp_path/'ticket.json'
    path.write_text(json.dumps(ticket), encoding='utf-8')
    inline = {k:v for k,v in ticket.items() if k not in ('dispatch_token','lease_token')}
    result = {**intent, 'ticket':inline, 'ticket_path':str(path), 'dispatch_action':'create',
              'responsible_thread_id':None, 'session_prompt':'synthetic request', 'suggested_title':'synthetic'}
    return result


def test_original_archive_matches_shared_redacted_binding_fields(delivery, tmp_path):
    _, scheduler, _, _ = delivery
    _, intent = begin(scheduler)
    result = archive(tmp_path, intent)
    assert verified_ticket(result, result['ticket_path'])[0] == intent['ticket']
    for key in ('input_key','revision','owner','issued_at'):
        wrong=copy.deepcopy(result);wrong['ticket'][key]='different'
        with pytest.raises(DispatchFailure):verified_ticket(wrong, result['ticket_path'])


def harness(result, host, preflight=None):
    calls=[]
    def started():calls.append('begin');return result
    def confirm(path,thread,ref):calls.append(('confirm',path,thread,ref));return {'receipt_accepted':1,'state_changed':1}
    def interrupted(path,note):calls.append(('interrupted',path))
    return calls,dict(host=host,archive_preflight=preflight or (lambda:calls.append('archive_preflight')),
                      begin=started,confirm=confirm,interrupted=interrupted,journal=lambda x:calls.append(('journal',x['phase'])))


def test_preflight_capability_and_archive_fail_before_begin():
    host=SimpleNamespace(preflight=lambda:{'create':False,'resume':True})
    calls,args=harness({},host)
    with pytest.raises(DispatchFailure):dispatch_once(**args)
    assert not calls
    host.preflight=lambda:{'create':True,'resume':True}
    def broken():raise OSError('synthetic archive unavailable')
    calls,args=harness({},host,broken)
    with pytest.raises(OSError):dispatch_once(**args)
    assert not calls


def test_begin_archive_failure_never_calls_host(delivery,tmp_path):
    _,s,_,_=delivery;_,intent=begin(s);result=archive(tmp_path,intent)
    result['ticket']['input_key']='stale'
    host=SimpleNamespace(preflight=lambda:{'create':True,'resume':True},dispatch=lambda **kw:pytest.fail('host called'))
    calls,args=harness(result,host)
    with pytest.raises(DispatchFailure):dispatch_once(**args)
    assert 'begin' in calls and ('journal','archive_invalid') in calls
    assert not any(isinstance(x,tuple) and x[0]=='confirm' for x in calls)


def test_one_host_create_and_original_ticket_confirm(delivery,tmp_path):
    _,s,_,_=delivery;_,intent=begin(s);result=archive(tmp_path,intent);seen=[]
    def host_call(**kw):seen.append(kw);return {'thread_id':'synthetic-responsible','host_ref':'synthetic-operation'}
    host=SimpleNamespace(preflight=lambda:{'create':True,'resume':True},dispatch=host_call)
    calls,args=harness(result,host);assert dispatch_once(**args)['receipt_accepted']==1
    assert len(seen)==1 and 'dispatch_token' not in seen[0]
    assert ('confirm',result['ticket_path'],'synthetic-responsible','synthetic-operation') in calls


def test_lost_host_response_only_records_interrupted(delivery,tmp_path):
    _,s,_,_=delivery;_,intent=begin(s);result=archive(tmp_path,intent)
    def broken(**kw):raise TimeoutError('synthetic missing response')
    host=SimpleNamespace(preflight=lambda:{'create':True,'resume':True},dispatch=broken)
    calls,args=harness(result,host)
    with pytest.raises(DispatchFailure):dispatch_once(**args)
    assert ('interrupted',result['ticket_path']) in calls
    assert not any(isinstance(x,tuple) and x[0]=='confirm' for x in calls)


def test_revision_advance_after_host_keeps_original_callback_identity(delivery,tmp_path):
    db,s,_,_=delivery;identity,intent=begin(s);result=archive(tmp_path,intent)
    def host_call(**kw):db.execute('UPDATE subtitle_delivery_jobs SET revision=revision+1 WHERE id=?',(identity,));return {'thread_id':'synthetic-thread'}
    host=SimpleNamespace(preflight=lambda:{'create':True,'resume':True},dispatch=host_call)
    _,args=harness(result,host)
    args['confirm']=lambda path,thread,ref:s.record_dispatch_receipt(verified_ticket(result,path)[0],'created',thread_id=thread)
    receipt=dispatch_once(**args)
    assert receipt['receipt_accepted']==0 and receipt['reason']=='revision_moved'
    assert s.get(identity)['dispatch_state']=='committing'


def test_observation_refresh_keeps_live_ticket_and_activity(delivery):
    db,s,ready,now=delivery;identity,intent=begin(s);before=s.get(identity)
    assessment=ready(db,42,1,{})
    ready.values[(42,1)]={**assessment,'review_notes':['a new diagnostic observation']}
    now[0]+=30;s.refresh();after=s.get(identity)
    assert after['revision']==before['revision'] and after['updated']==before['updated']
    assert after['payload']['latest_assessment']['assessment']['review_notes']==['a new diagnostic observation']
    receipt=s.record_dispatch_receipt(intent['ticket'],'created',thread_id='synthetic-thread')
    assert receipt['receipt_accepted']==1 and receipt['state_changed']==1


@pytest.mark.parametrize('change',['input','scope','watched'])
def test_real_authority_changes_still_invalidate_live_ticket(delivery,change):
    db,s,ready,now=delivery;identity,intent=begin(s);before=s.get(identity)
    if change=='input':ready.values[(42,1)]={**ready(db,42,1,{}),'input_signature':'new-synthetic-input'}
    if change=='scope':db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    if change=='watched':db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,1,1)')
    now[0]+=30;s.refresh();after=s.get(identity)
    assert after['revision']>before['revision']
    receipt=s.record_dispatch_receipt(intent['ticket'],'created',thread_id='synthetic-thread')
    assert receipt['receipt_accepted']==0 and receipt['reason']=='revision_moved'
    assert after['dispatch_state']=='committing'
