"""Personal selection -> verified first-episode research, with no real downloaders."""
import datetime,json,time
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from anime.db import Store
from anime.season import install_season
from test_reliability import setup
from test_source_review import ready_review

TODAY=datetime.date(2026,10,7)


def install_pipeline(db,e,prepare=None,listing=None):
    db.set('auto_quarter_start','2026-10');db.set('auto_quarter_enabled',True)
    db.set('source_review_new_shows',True)
    db.execute("UPDATE shows SET quarter='2026-10' WHERE id=42")
    prepare=prepare or Mock(return_value={'mapping':db.mapping(42),'warning':'','preview':['valid'],'inventory_count':0})
    tick=install_season(FastAPI(),db,e,listing or (lambda **kw:[db.show(42)]),prepare,Mock())
    enroll=e.enroll_source_reviews
    e.enroll_source_reviews=lambda sid=None:enroll(TODAY,sid)
    e.auto_tick=lambda force=False:tick(TODAY,force)
    return tick,prepare


def test_click_wish_requests_persistent_check_and_blocks_ordinary_ingestion_immediately(setup):
    db,q,e,r,c=ready_review(setup)
    db.execute("UPDATE shows SET selected=0,state='wish',auto_download=1 WHERE id=42")
    _,prepare=install_pipeline(db,e);db.set('auto_retry_42',time.time()+1800)
    e.state(42,'wish')
    assert db.show(42)['selected']==1 and db.show(42)['state']=='wish'
    assert db.get('check_requested')==1 and db.get('auto_retry_42')==0
    assert e.source_review.policy(42)['mode']=='sampling'
    e.ingest(c)
    assert db.one('SELECT status FROM candidates')['status']=='source_wait'
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM tasks')
    # An identical button request does not endlessly force every subscription.
    e.state(42,'wish');assert db.get('check_requested')==1
    reopened=Store(db.path);assert reopened.get('check_requested')==1
    assert not prepare.called and q.adds==0


def test_new_wish_is_verified_then_automatically_sampled_without_changing_watch_state(setup):
    db,q,e,r,c=ready_review(setup)
    db.execute("UPDATE shows SET selected=0,state='wish',authorized=0,auto_download=0 WHERE id=42")
    tick,prepare=install_pipeline(db,e)
    e.state(42,'wish');assert not db.show(42)['authorized']
    tick(TODAY)
    assert prepare.call_count==1 and db.show(42)['authorized']==1
    assert db.show(42)['state']=='wish' and e.source_review.policy(42)['mode']=='sampling'
    e.sources.rss=Mock(return_value=True);e.sources.archive=Mock()
    db.execute('UPDATE sources SET next_attempt=?',(time.time()+1800,))
    e.cycle()
    assert e.sources.rss.call_count==2 # Mikan and the supplemental DMHY title search.
    assert db.one('SELECT purpose FROM tasks')['purpose']=='source_sample'
    assert db.one('SELECT state FROM thunder_queue')['state']=='pending'
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches') and q.adds==0
    assert db.get('check_completed')==db.get('check_requested')==1


def test_click_during_torrent_fetch_cannot_commit_an_ordinary_version_before_review(setup):
    db,q,e,r,c=ready_review(setup)
    db.execute("UPDATE shows SET selected=0,state='wish',auto_download=1 WHERE id=42")
    install_pipeline(db,e)
    fetch=e.sources.torrent
    def click(candidate):
        data=fetch(candidate);e.state(42,'wish');return data
    e.sources.torrent=click;e.ingest(c)
    assert e.source_review.policy(42)['mode']=='sampling'
    assert db.one('SELECT status FROM candidates')['status']=='source_wait'
    assert not db.rows('SELECT * FROM tasks') and not db.rows('SELECT * FROM episodes') and q.adds==0


def test_wanted_preparation_precedes_unselected_quarter_catalog_and_survives_catalog_failure(setup):
    db,q,e,c=setup
    db.execute("UPDATE shows SET selected=1,state='wish',authorized=0 WHERE id=42")
    for sid in (1,2,3):db.upsert_subject({'id':sid,'name':'Example'+str(sid)},'2026-10')
    prepare=Mock(side_effect=lambda sid:{'mapping':db.mapping(42),'warning':'','preview':['valid'],'inventory_count':0})
    tick,_=install_pipeline(db,e,prepare,lambda **kw:db.rows('SELECT * FROM shows ORDER BY id'))
    e.catalog.sync_quarter.side_effect=TimeoutError('catalog unavailable')
    tick(TODAY)
    assert prepare.call_args_list[0].args==(42,) and prepare.call_count==2
    assert db.show(42)['authorized'] and e.source_review.policy(42)['mode']=='sampling'
    assert q.adds==0


@pytest.mark.parametrize('protection',['dropped','paused','completed','cleanup_hold','inventory','warning','no_release'])
def test_automatic_wish_pipeline_keeps_existing_gates(setup,protection):
    db,q,e,c=setup
    db.execute("UPDATE shows SET selected=1,state='wish',authorized=0 WHERE id=42")
    tick,prepare=install_pipeline(db,e)
    if protection in ('dropped','paused','completed'):db.execute('UPDATE shows SET state=? WHERE id=42',(protection,))
    elif protection=='cleanup_hold':db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    else:prepare.return_value.update({'inventory':{'inventory_count':1},'warning':{'warning':'season mismatch'},'no_release':{'preview':[]}}[protection])
    before=db.show(42);tick(TODAY)
    assert db.show(42)==before and e.source_review.policy(42)=={}
    assert not db.rows('SELECT * FROM tasks') and q.adds==0


@pytest.mark.parametrize('platform,date,selected,continuing,expected',[
    ('TV','2026-10-01',1,False,True),('WEB','2026-10-01',1,False,True),
    ('Movie','2026-10-01',1,False,False),('OVA','2026-10-01',1,False,False),
    ('TV','2026-07-01',1,False,False),('TV','2027-01-01',1,False,False),
    ('TV','2026-10-01',0,False,False),('TV','2026-10-01',1,True,False),
])
def test_explicit_current_import_can_enroll_without_assigning_a_quarter(setup,platform,date,selected,continuing,expected):
    from anime.app import create_app
    db,q,e,c=setup
    db.upsert_subject({'id':42,'type':2,'name':'アニメ','platform':platform,'date':date})
    db.execute("UPDATE shows SET quarter='',selected=? WHERE id=42",(selected,))
    if continuing:db.execute("INSERT INTO quarter_members VALUES(42,'2026-10','continuing','verified')")
    db.set('auto_quarter_start','2026-10');db.set('auto_quarter_enabled',True);db.set('source_review_new_shows',True)
    create_app(db,e,False);before=db.show(42)
    e.enroll_source_reviews(TODAY)
    assert bool(e.source_review.policy(42))==expected
    assert db.show(42)==before and q.adds==0 and not db.rows('SELECT * FROM tasks')


def test_automatic_sample_cap_proceeds_and_later_same_family_does_not_duplicate(setup):
    db,q,e,r,c=ready_review(setup);add=setup[3]
    for n in range(13):add(f'[Group{n}] Example S01E01 [1080p][CHS]',url=f'https://mikanani.me/{n}.torrent')
    # Use distinct fake torrents so hash dedupe does not hide family duplication.
    from test_reliability import torrent
    e.sources.torrent=lambda candidate:torrent(1,name=f'Example S01E01-{candidate["id"]}.mkv')
    result=e.source_review.start(42)
    assert len(result['samples'])==12 and result['sampling_limit']['available_families']==14
    assert result['sampling_limit']['remaining_families']==2
    before=db.rows('SELECT * FROM tasks')
    add('[Group] Example S01E01 [1080p][CHT]',url='https://mikanani.me/traditional.torrent')
    e.source_review.start(42)
    assert db.rows('SELECT * FROM tasks')==before and len(db.rows('SELECT * FROM source_samples'))==12
    assert not db.rows('SELECT * FROM episodes') and q.adds==0


def test_disabled_quarter_does_not_turn_click_into_authorization(setup):
    db,q,e,c=setup
    _,prepare=install_pipeline(db,e)
    db.set('auto_quarter_enabled',False);db.set('auto_discovery_enabled',False)
    db.execute("UPDATE shows SET selected=0,state='wish',authorized=0 WHERE id=42")
    e.state(42,'wish');e.auto_tick()
    assert not db.show(42)['authorized'] and not e.source_review.policy(42) and not prepare.called and q.adds==0
