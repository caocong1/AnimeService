import time
import pytest
from fastapi.testclient import TestClient
from anime.app import create_app
from anime.download_gaps import inspect_missing,reconcile_missing
from anime.source_review import family
from anime.viewing_workflow import KEY,POLICY
from test_reliability import setup,torrent


def configured(setup,mode='selected'):
    db,q,e,c=setup
    db.set(KEY,POLICY);db.set('subtitle_studio_enabled',True)
    db.set('subtitle_clean_media_preferred',True)
    db.set('play_completed_premiere_enabled',True)
    db.execute("INSERT INTO sources(id,show_id,kind,url,enabled) VALUES(1,42,'mikan','https://mikanani.me/rss',1)")
    p=db.path.parent/'first.mkv';p.write_bytes(b'x'*100)
    db.execute("INSERT INTO episodes(show_id,episode,path,size,status) VALUES(42,1,?,100,'complete')",(str(p),))
    title='[Group] Example S01E02 [1080p][CHS][硬字幕]'
    key=family(title)['key']
    e.source_review.save(42,{'mode':mode,'primary':key if mode=='selected' else '',
        'backups':[],'playback_family':key,'wait_hours':24})
    db.execute("INSERT INTO airings VALUES(42,2,'2000-01-01','')")
    e.sources.data=torrent((2,))
    return db,q,e,c,title


@pytest.mark.parametrize('mode',['selected','sampling'])
def test_ordinary_continuation_downloads_without_weakening_clean_base_or_review(setup,mode):
    db,q,e,c,title=configured(setup,mode);candidate=c(title)
    before=db.show(42);policy=e.source_review.policy(42)
    assert not e.source_review.permits(candidate)
    assert e.source_review.permits(candidate,ordinary=True)
    e.ingest(candidate)
    assert q.adds==1 and q.starts and db.one('SELECT status FROM episodes WHERE episode=2')['status']=='downloading'
    assert db.show(42)==before and e.source_review.policy(42)==policy
    assert not db.rows('SELECT * FROM watches') and not db.rows('SELECT * FROM source_samples')


def test_transport_wrapper_matches_existing_family_but_not_other_publishers(setup):
    db,q,e,c,title=configured(setup,'sampling')
    wrapped=c('[搬运]'+title)
    assert e.source_review.permits(wrapped,ordinary=True)
    assert not e.source_review.permits(c('[Other]'+title,url='https://mikanani.me/other'),ordinary=True)
    db.set('source_publisher_blacklist',[{'publisher':'Group','aliases':['Group'],'reason':'广告'}])
    assert not e.source_review.permits(wrapped,ordinary=True)
    e.ingest(wrapped);assert not q.adds


def test_persisted_wrapped_family_matches_unwrapped_title_without_key_migration(setup):
    db,q,e,c,title=configured(setup,'sampling');first=c('[搬運]'+title.replace('E02','E01'))
    key=family(first['title'])['key']
    db.execute("INSERT INTO source_samples(candidate_id,show_id,family,state) VALUES(?,42,?,'ready')",(first['id'],key))
    policy=e.source_review.policy(42);policy['playback_family']=key;e.source_review.save(42,policy)
    assert e.source_review.permits(c(title,url='https://mikanani.me/two'),ordinary=True)
    assert not e.source_review.permits(c('[搬運]'+title.replace('[Group]','[Other]'),url='https://mikanani.me/other-wrapper'),ordinary=True)
    assert e.source_review.policy(42)==policy


def test_maintenance_retries_known_candidates_without_waiting_for_feed_success(setup):
    db,q,e,c,title=configured(setup);candidate=c(title)
    db.execute("UPDATE candidates SET status='source_wait',reason='old generic reason'")
    db.execute("UPDATE sources SET error='HTTP 522',last_attempt=?",(time.time(),))
    reconcile_missing(e);reconcile_missing(e)
    assert q.adds==1 and len(db.rows('SELECT * FROM tasks'))==1
    assert db.get('download_gaps')['shows']=={}
    assert not db.rows('SELECT * FROM watches')


@pytest.mark.parametrize('protection',['watched','unauthorized','paused','hold','disabled_source'])
def test_missing_reconcile_preserves_scope_watches_and_source_toggles(setup,protection):
    db,q,e,c,title=configured(setup);c(title)
    if protection=='watched':db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    if protection=='unauthorized':db.execute('UPDATE shows SET authorized=0')
    if protection=='paused':db.execute("UPDATE shows SET state='paused'")
    if protection=='hold':db.execute('UPDATE shows SET cleanup_hold=1')
    if protection=='disabled_source':db.execute('UPDATE sources SET enabled=0')
    reconcile_missing(e)
    assert not q.adds and not db.rows('SELECT * FROM tasks')


def test_reports_distinguish_not_found_policy_and_retry_and_are_read_only_on_pages(setup):
    db,q,e,c,title=configured(setup)
    report,_=inspect_missing(e,42);assert report['2']['code']=='source_not_found'
    other=c('[Other] Example S01E02 [1080p][CHS]')
    report,_=inspect_missing(e,42);assert report['2']['code']=='no_qualified_backup'
    candidate=c(title,url='https://mikanani.me/good')
    db.execute("UPDATE candidates SET status='retry',reason='网络超时' WHERE id=?",(candidate['id'],))
    db.set('candidate_retry_'+str(candidate['id']),{'next':time.time()+600})
    reconcile_missing(e)
    assert not q.adds
    report=db.get('download_gaps')['shows']['42']['2'];assert report['code']=='download_retry' and report['retry_at']>time.time()
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        before=db.get('download_gaps')
        listing=client.get('/api/shows?scope=home').json()[0]
        detail=client.get('/api/shows/42').json()
        assert listing['eps'][1]['download']==detail['board']['eps'][1]['download']==report
        assert db.get('download_gaps')==before and not q.adds


def test_ambiguous_submission_is_reconciled_instead_of_duplicated(setup):
    db,q,e,c,title=configured(setup);c(title);q.ambiguous=True
    reconcile_missing(e);reconcile_missing(e)
    assert q.adds==1 and len(db.rows('SELECT * FROM tasks'))==1


def test_reserved_task_failure_reason_is_visible_without_resubmission(setup):
    db,q,e,c,title=configured(setup);c(title);reconcile_missing(e)
    db.execute("UPDATE tasks SET status='review',error='提交回执未知；先按 hash 对账'")
    before=q.adds
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        listing=client.get('/api/shows?scope=home').json()[0]
        detail=client.get('/api/shows/42').json()
        note=listing['eps'][1]['download']
        assert note==detail['board']['eps'][1]['download']
        assert note['code']=='task_review' and '回执未知' in note['reason']
    reconcile_missing(e);assert q.adds==before


def test_explicit_backup_request_is_per_episode_and_keeps_permanent_policy(setup):
    db,q,e,c,title=configured(setup)
    policy=e.source_review.policy(42);policy.update(primary='other-reviewed-family',backups=[family(title)['key']])
    e.source_review.save(42,policy);candidate=c(title)
    assert e.source_review.decision(candidate,ordinary=True)['code']=='primary_window'
    with TestClient(create_app(db,e,False),base_url='http://127.0.0.1:4871') as client:
        token=client.get('/api/bootstrap').json()['token']
        response=client.post('/api/shows/42/source-review/backup-now',json={'episode':2},headers={'X-Anime-Token':token})
        assert response.status_code==200
        assert e.source_review.policy(42)==policy
        assert e.source_review.permits(candidate,ordinary=True)
        assert not e.source_review.permits(candidate)
        later=c(title.replace('E02','E03'),url='https://mikanani.me/three')
        assert not e.source_review.permits(later,ordinary=True)
        reconcile_missing(e);assert q.adds==1
        with pytest.raises(ValueError,match='已有任务'):e.source_review.use_backup_now(42,2)


def test_explicit_backup_request_cannot_grant_unreviewed_or_watched_episode(setup):
    db,q,e,c,title=configured(setup);c(title)
    with pytest.raises(ValueError,match='没有已评审备源'):e.source_review.use_backup_now(42,2)
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(42,2,1)')
    with pytest.raises(ValueError,match='已看集'):e.source_review.use_backup_now(42,2)
    assert not db.get('download_backup_now_42_2') and not q.adds
