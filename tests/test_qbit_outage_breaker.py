"""qB going away mid-pass stops further per-task qB calls; ingestion does not fetch torrents while qB is down."""
import time
from unittest.mock import Mock
from test_reliability import setup,torrent
from anime.qbit import QbitUnavailable


def three_tasks(setup):
    db,q,e,c=setup
    for n in (1,2,3):
        e.sources.data=torrent((n,));e.ingest(c(f'[Group] Example S01E{n:02} [1080p][CHS]',url=f'https://mikanani.me/{n}.torrent'))
    assert q.adds==3
    return db,q,e,c


def test_qbit_lost_mid_pass_is_recorded_once_and_skips_the_rest(setup):
    db,q,e,c=three_tasks(setup)
    db.execute('UPDATE shows SET authorized=0 WHERE id=42')  # every task takes the hold() path, one qB call each
    info=q.info;calls=[]
    def failing(h=None):
        if h is None:return info()
        calls.append(h);raise QbitUnavailable('qBittorrent本机接口未监听（127.0.0.1:4870）；需恢复原下载器，任务与回执已保留')
    q.info=failing
    e.reconcile()
    assert len(calls)==1
    assert len(db.rows("SELECT * FROM events WHERE scope LIKE 'task:%' AND level='error'"))==1
    assert len([k for k in db.rows("SELECT key FROM kv WHERE key LIKE 'task_failure_%'")])==1
    health=db.get('qbit_health')
    assert health['ok'] is False and '未监听' in health['error']


def test_other_qbit_errors_keep_per_task_handling(setup):
    from anime.net import NetworkError
    db,q,e,c=three_tasks(setup)
    db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    info=q.info;calls=[]
    def failing(h=None):
        if h is None:return info()
        calls.append(h);raise NetworkError('qBittorrent torrents/info HTTP 500')
    q.info=failing
    e.reconcile()
    assert len(calls)==3 and db.get('qbit_health')['ok'] is True


def test_ingest_waits_without_fetching_while_qbit_is_known_down(setup):
    db,q,e,c=setup;cand=c()
    e.sources=Mock(wraps=e.sources)
    db.set('qbit_health',{'ok':False,'time':time.time(),'error':'qB未监听'})
    e.ingest_show(42)
    e.sources.torrent.assert_not_called()
    assert db.one('SELECT status FROM candidates WHERE id=?',(cand['id'],))['status']=='discovered'
    assert db.get('candidate_retry_'+str(cand['id'])) is None and q.adds==0
    db.set('qbit_health',{'ok':False,'time':time.time()-120,'error':'qB未监听'})  # stale: try again
    e.ingest_show(42)
    assert q.adds==1


def test_thunder_first_ingest_is_not_gated_by_qbit_health(setup):
    db,q,e,c=setup;c()
    db.set('downloader_preference','thunder_first')
    db.set('qbit_health',{'ok':False,'time':time.time(),'error':'qB未监听'})
    assert not e.qbit_recently_down()
