import json
import time
from pathlib import Path

import pytest

from anime.subtitle_dispatch_fence import PREFIX, check_output, isolate
from test_subtitle_delivery import delivery, begin


def test_stopped_resume_is_fenced_without_releasing_identity(delivery, monkeypatch):
    db, scheduler, _, clock = delivery
    identity, intent = begin(scheduler)
    ticket = intent['ticket']
    thread, turn = 'stopped-owner', 'original-turn'
    # Represent a previously acknowledged, now stopped owner's resume.
    row = scheduler.get(identity)
    payload = row['payload']
    payload.update(auto_recovery_turn=turn, owner_stopped_writing=True, recovery_class='system')
    payload['assessment']['material_refs']=[{'kind':'completed_media','target':{'path':'fixed.mkv'}}]
    db.execute("UPDATE subtitle_delivery_jobs SET thread_id=?,payload=?,lease_expires=0,updated=0 WHERE id=?",
               (thread,json.dumps(payload),identity))
    ticket.update(dispatch_action='reuse',thread_id=thread)
    from anime.subtitle_delivery import _canonical, _digest
    db.execute("UPDATE subtitle_delivery_events SET payload=? WHERE job_id=? AND kind='dispatch-ticket-issued'",
               (json.dumps({'ticket_digest':_digest(_canonical(ticket))}),identity))
    scheduler.record_dispatch_receipt(ticket,'interrupted',evidence={'note':'host interrupted'})
    original = scheduler.get(identity)
    host = {'thread':{'cwd':str(db.path.parent.parent),'id':thread,'hostId':'local','status':{'type':'notLoaded'}},
            'turns':[{'id':turn,'status':'completed','completedAt':1,'items':[]}]}
    path=db.path.parent/'native.json'
    path.write_text(json.dumps(host))
    observe='anime.subtitle_manual_continuation._observe_related_processes'
    monkeypatch.setattr(observe, lambda *args:{'related_processes':[123]})
    with pytest.raises(ValueError,match='writer'):
        isolate(scheduler,identity,row['revision'],path,time.time(),apply=True)
    monkeypatch.setattr(observe, lambda *args:{'related_processes':[]})
    isolate(scheduler,identity,row['revision'],path,time.time(),apply=True)
    assert scheduler.get(identity)==original
    assert scheduler.snapshot()['capacity']['available']==1
    with pytest.raises(ValueError,match='暂停'):
        check_output(db,42,1)
    with pytest.raises(ValueError,match='reconcile|protected|already|lease|active'):
        scheduler.reserve(identity,'new',row['revision'])
    other,_=begin(scheduler,43,1)
    late=scheduler.record_dispatch_receipt(ticket,'created',thread_id=thread)
    assert late['reason']=='production_capacity_full' and not late['state_changed']
    assert scheduler.get(identity)==original
