import json
from pathlib import Path
import pytest
from test_reliability import setup,torrent
from test_subtitle_studio import ready
from anime.viewing_workflow import POLICY,KEY,queue_snapshot
from anime.native_indexes import clean_required,task_clean_blocker
from anime.research_tasks import sample_allowed


def split(setup):
    db,q,e,r,c=ready(setup)
    db.set(KEY,POLICY);db.set('downloader_preference','qbit')
    db.set('subtitle_clean_media_preferred',True)
    return db,q,e,r,c


def candidate(setup,number=0,mode='RAW',episode=2):
    return setup[3](f'[Base{number}] Example S01E{episode:02} [1080p][{mode}]',
                    url=f'https://mikanani.me/base{number}.torrent')


def studio_candidate(e,c):
    return next(row for row in e.subtitle_studio.candidates(42) if row['id']==c['id'])


def test_wish_keeps_chinese_premiere_without_new_subtitle_production(setup):
    db,q,e,r,_=split(setup)
    db.execute("UPDATE shows SET state='wish',auto_download=1 WHERE id=42")
    assert not clean_required(db,42)
    e.source_review.ensure(42,False)
    db.execute("INSERT INTO subtitle_studio_jobs VALUES(42,1,'waiting_review',?,'',0)",
               (json.dumps({'season':1,'draft_ids':[],'workbook':None,'target':None}),))
    before=db.one('SELECT * FROM subtitle_studio_jobs')
    with pytest.raises(ValueError,match='个人在追'):e.subtitle_studio.ensure(42,2,True)
    e.subtitle_studio.discover()
    assert q.adds==0 and db.one('SELECT * FROM subtitle_studio_jobs')==before
    assert queue_snapshot(db)['summary']['wish']['eligible']==1


def test_clean_bases_are_bounded_by_hash_and_preserve_all_tasks(setup):
    db,q,e,r,_=split(setup)
    assert clean_required(db,42)
    for number in range(3):
        c=studio_candidate(e,candidate(setup,number))
        e.sources.data=torrent((2,)).replace(b'x'*20,bytes([120+number])*20)
        if number<2:e.subtitle_studio.queue(c)
        else:
            before=db.rows('SELECT * FROM tasks ORDER BY hash')
            with pytest.raises(ValueError,match='主/对照'):e.subtitle_studio.queue(c)
            assert db.rows('SELECT * FROM tasks ORDER BY hash')==before
    assert q.adds==2 and len(db.rows('SELECT * FROM subtitle_samples'))==2
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')


def test_normal_chinese_fallback_and_explicit_reference_have_distinct_roles(setup):
    db,q,e,r,_=split(setup)
    c=studio_candidate(e,candidate(setup,0,'硬字幕 CHS'))
    e.sources.data=torrent((2,))
    with pytest.raises(ValueError,match='生肉'):e.subtitle_studio.queue(c)
    e.subtitle_studio.queue(c,'subtitle_reference')
    task=db.one('SELECT * FROM tasks')
    assert task['purpose']=='subtitle_sample' and not task_clean_blocker(db,task)
    assert json.loads(db.one('SELECT evidence FROM subtitle_samples')['evidence'])['workflow_role']=='subtitle_reference'
    ordinary={**task,'purpose':'episode','acknowledged':0,'status':'intent'}
    assert not task_clean_blocker(db,ordinary)
    assert q.adds==1 and not db.rows('SELECT * FROM episodes')


def test_reference_premiere_can_be_archived_without_overwriting_regular_episode(setup):
    db,q,e,r,_=split(setup)
    c=studio_candidate(e,candidate(setup,0,'硬字幕 CHS',1))
    db.execute("INSERT INTO episodes(show_id,episode,path,status) VALUES(42,1,'original.mkv','complete')")
    before=db.one('SELECT * FROM episodes');e.sources.data=torrent((1,))
    e.subtitle_studio.queue(c,'subtitle_reference')
    assert q.adds==1 and db.one('SELECT * FROM episodes')==before
    assert list(json.loads(db.one('SELECT selection FROM tasks')['selection'])['selected'])==['1']


def test_premiere_clean_sampling_also_respects_the_shared_budget(setup):
    db,q,e,r,_=split(setup)
    for number in range(3):
        c=candidate(setup,number,episode=1)
        e.sources.data=torrent((1,)).replace(b'x'*20,bytes([120+number])*20)
        e.source_review.start(42,[c['id']])
    assert q.adds==2 and len(db.rows('SELECT * FROM tasks'))==2
    last=db.one('SELECT * FROM source_samples WHERE candidate_id=?',(c['id'],))
    assert last['state']=='error' and '主/对照' in last['error']


def test_reference_budget_preserves_two_completed_or_pending_inputs(setup):
    db,q,e,r,_=split(setup)
    for number in range(3):
        c=studio_candidate(e,candidate(setup,number,'硬字幕 CHS'))
        e.sources.data=torrent((2,)).replace(b'x'*20,bytes([120+number])*20)
        if number<2:e.subtitle_studio.queue(c,'subtitle_reference')
        else:
            before=db.rows('SELECT * FROM subtitle_samples ORDER BY candidate_id')
            with pytest.raises(ValueError,match='两份额外参考'):e.subtitle_studio.queue(c,'subtitle_reference')
            assert db.rows('SELECT * FROM subtitle_samples ORDER BY candidate_id')==before
    assert q.adds==2 and not db.rows('SELECT * FROM episodes')


def test_state_change_stops_future_samples_but_reconciles_existing_receipt(setup):
    db,q,e,r,_=split(setup);c=studio_candidate(e,candidate(setup));e.sources.data=torrent((2,))
    e.subtitle_studio.queue(c);task=db.one('SELECT * FROM tasks')
    db.execute("UPDATE shows SET state='wish',auto_download=1 WHERE id=42")
    assert sample_allowed(db,task)
    selected=json.loads(task['selection'])['selected']['2']
    p=Path(task['save_path'])/selected['name'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x'*selected['size'])
    q.fs[task['hash']][0]['progress']=1
    stops=list(q.stops);e.reconcile()
    assert q.adds==1 and q.stops==stops and db.one('SELECT status FROM tasks')['status']=='complete'
    assert Path(db.one('SELECT path FROM subtitle_samples')['path']).is_file()
    db.execute("UPDATE tasks SET acknowledged=0,status='intent'")
    assert not sample_allowed(db,db.one('SELECT * FROM tasks'))


def test_uncertain_submission_stays_query_only_after_state_change(setup):
    db,q,e,r,_=split(setup);e.sources.data=torrent((2,));q.ambiguous=True
    with pytest.raises(TimeoutError):e.subtitle_studio.queue(studio_candidate(e,candidate(setup)))
    task=db.one('SELECT * FROM tasks');q.tasks.clear()
    db.execute("UPDATE shows SET state='wish',auto_download=1 WHERE id=42")
    assert task['status']=='uncertain' and sample_allowed(db,task)
    e.reconcile()
    assert q.adds==1 and db.one('SELECT status FROM tasks')['status']=='uncertain'


def test_ineligible_watching_never_bypasses_independent_authorization(setup):
    db,q,e,r,_=split(setup);db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    snapshot=queue_snapshot(db)
    assert snapshot['summary']['watching']=={'personal':1,'eligible':0}
    assert snapshot['queues']['watching'][0]['scope_blocker']
    with pytest.raises(ValueError,match='独立授权'):e.subtitle_studio.ensure(42,2,True)
    assert q.adds==0


def test_acquisition_api_rechecks_lane_episode_and_session(setup):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db,q,e,r,_=split(setup);c=candidate(setup,mode='硬字幕 CHS');e.sources.data=torrent((2,))
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        route='/api/subtitle-studio/42/2/acquire';body={'candidate_ids':[c['id']],'role':'subtitle_reference'}
        assert client.post(route,json=body).status_code==403
        token=client.get('/api/bootstrap').json()['token'];headers={'X-Anime-Token':token}
        assert client.post('/api/subtitle-studio/42/1/acquire',json=body,headers=headers).status_code==400
        assert client.post(route,json=body,headers=headers).status_code==200
        db.execute("UPDATE shows SET state='wish',auto_download=1 WHERE id=42")
        assert client.post(route,json=body,headers=headers).status_code==400
        assert client.get('/api/viewing-workflow').json()['summary']['wish']['personal']==1
    assert q.adds==1
