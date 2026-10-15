import time
from pathlib import Path
from unittest.mock import Mock
import pytest
from test_reliability import setup
from test_source_review import ready_review
from anime.download_recovery import recover_downloads


def test_new_samples_use_existing_qbit_and_preserve_personal_state(setup):
    db,q,e,r,c=ready_review(setup);db.set('downloader_preference','qbit');db.set('thunder_ui_enabled',False)
    before=db.show(42);q.ensure_running=Mock()
    e.source_review.start(42)
    t=db.one('SELECT * FROM tasks')
    assert t['provider']=='qbit' and t['purpose']=='source_sample'
    assert q.adds==1 and q.starts==[t['hash']]
    assert Path(t['save_path']).is_relative_to(e.library)
    assert not db.rows('SELECT * FROM thunder_queue') and not db.rows('SELECT * FROM episodes')
    assert not db.rows('SELECT * FROM watches') and db.show(42)==before
    e.source_review.start(42);assert q.adds==1
    q.ensure_running.assert_not_called()


def test_preference_switch_moves_safe_pending_work_without_ui_fault(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    db.set('downloader_preference','qbit');db.set('thunder_ui_enabled',False)
    q.ensure_running=Mock();recover_downloads(e)
    assert q.adds==1 and db.one('SELECT provider FROM tasks')['provider']=='qbit'
    assert db.one('SELECT state FROM thunder_queue')['state']=='cancelled'
    recover_downloads(e);assert q.adds==1
    q.ensure_running.assert_not_called()


@pytest.mark.parametrize('block',['dialog','review','committing','lease','ack','foreign_qbit','foreign_thunder','offline','unselected','blacklist'])
def test_switch_never_reposts_unsafe_work(setup,block):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);h=db.one('SELECT hash FROM tasks')['hash']
    db.set('downloader_preference','qbit');db.set('thunder_ui_enabled',False)
    if block=='dialog':db.set('thunder_dialog_hold_'+h,{'unconfirmed':True})
    if block=='review':db.execute("UPDATE tasks SET status='review'")
    if block=='committing':db.execute('UPDATE thunder_queue SET committing=1')
    if block=='lease':db.execute("UPDATE thunder_queue SET lease='test',expires=?",(time.time()+60,))
    if block=='ack':db.execute('UPDATE tasks SET acknowledged=1')
    if block=='foreign_qbit':q.tasks[h]={'hash':h,'category':'Other','tags':'Other'}
    if block=='foreign_thunder':r.by_hash=lambda _: [{'TaskId':9}]
    if block=='offline':q.online=False
    if block=='unselected':db.execute('UPDATE shows SET selected=0')
    if block=='blacklist':
        db.set('source_publisher_blacklist',[{'publisher':'Skymoon-Raws','aliases':['Skymoon-Raws','Skymoon'],'reason':'正片广告'}])
        db.execute("UPDATE tasks SET title='[Skymoon-Raws] Example S01E01 [1080p][CHS]'")
    recover_downloads(e)
    assert q.adds==0 and not q.starts and db.one('SELECT provider FROM tasks')['provider']=='thunder'


def test_disabled_ui_rejects_preference_changed_after_claim(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42);job=e.thunder.claim()
    db.set('downloader_preference','qbit');db.set('thunder_ui_enabled',False)
    assert e.thunder.claim() is None
    with pytest.raises(ValueError,match='停用'):e.thunder.preflight(job['hash'],job['lease'])
    assert db.one('SELECT committing FROM thunder_queue')['committing']==0


def test_new_regular_qbit_intent_checks_other_downloader_before_add(setup):
    db,q,e,c=setup;e.thunder.reader.by_hash=Mock(return_value=[{'TaskId':9}])
    e.ingest(c());assert q.adds==0 and not db.rows('SELECT * FROM tasks')


def test_storage_failure_before_add_keeps_retryable_intent(setup):
    db,q,e,c=setup;q.check_storage=Mock(side_effect=ValueError('wrong storage'))
    e.ingest(c());assert q.adds==0 and db.one('SELECT status FROM tasks')['status']=='intent'


def test_invalid_manifest_does_not_starve_next_safe_item(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    first=db.one('SELECT * FROM tasks');(e.cache/(first['hash']+'.torrent')).write_bytes(b'bad')
    e.sources.data=e.sources.data.replace(b'i100e',b'i101e')
    second=setup[3]('[Other] Example S01E01 [1080p][CHS]',source=1,url='https://example.invalid/other')
    e.source_review.start(42)
    db.set('downloader_preference','qbit');db.set('thunder_ui_enabled',False)
    recover_downloads(e,limit=1)
    assert q.adds==1 and db.one('SELECT provider FROM tasks WHERE hash=?',(first['hash'],))['provider']=='thunder'
    assert db.get('qbit_handoff_check_'+first['hash'])['error']
