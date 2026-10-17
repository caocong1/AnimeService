"""Production-observed failures and boundary cases, using only synthetic data."""
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from unittest.mock import Mock
import pytest
from test_reliability import setup
from test_source_sites import tokyo_sources,clock,response,fresh
from test_native_indexes import rss
from anime.sources import Sources,feed_items,request_url
from anime.net import NetworkError,RequestDeferred,Web,retry_after


@pytest.mark.parametrize('status',[500,501,507,520,522,524,599])
def test_all_server_errors_are_site_wide(status):
    assert NetworkError('public host HTTP '+str(status),status).site_wide


@pytest.mark.parametrize('site,kind,body',[
    ('tokyotosho','tokyotosho',b''),('tokyotosho','tokyotosho',b'  '),
    ('tokyotosho','tokyotosho',b'</rss>'),('nyaa.si','nyaa',b'</channel> </rss>'),
    ('mikanani.me','mikan',b'</channel> </rss>')])
def test_empty_exception_is_only_the_observed_tokyo_fragment(site,kind,body):
    with pytest.raises(NetworkError):feed_items(site,response(body,'text/html'),kind)


def test_alias_rewrite_cannot_remove_invalid_url_components():
    for url in ['https://secret@www.tokyotosho.info/rss.php','https://www.tokyotosho.info:444/rss.php']:
        with pytest.raises(ValueError):request_url(url,{})


def test_retry_after_supports_dates_and_does_not_cut_long_waits(monkeypatch,clock):
    monkeypatch.setattr('anime.net.time',clock)
    for value,expected in [('172800',172800),(formatdate(clock.now+7200,usegmt=True),7200),
                           (formatdate(clock.now-100,usegmt=True),0),('nonsense',0),('-1',0)]:
        assert retry_after(Mock(headers={'Retry-After':value}))==expected


@pytest.mark.parametrize('status',[403,429])
def test_restricted_site_does_not_rotate_aliases(setup,clock,status):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.side_effect=NetworkError('restricted',status)
    assert not Sources(db,web).rss(rows[0])
    state=db.get('site_health_tokyotosho')
    assert state['host']=='www.tokyotosho.info' and state['next_attempt']==clock.now+60


def test_two_concurrent_polls_only_one_sends_and_stats_merge(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.return_value=response(rss(kind='tokyotosho'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda row:Sources(db,web).rss(row),rows))
    assert sorted(results)==[False,True] and web.fetch.call_count==1
    stats=db.get('site_health_tokyotosho')['stats']
    assert stats=={'http_attempts':1,'polls':1,'success':1,'deferred':1}


def test_remember_failure_is_not_also_counted_as_success(setup,clock,monkeypatch):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.return_value=response(rss(kind='tokyotosho'))
    source=Sources(db,web);monkeypatch.setattr(source,'remember',Mock(side_effect=ValueError('synthetic')))
    assert not source.rss(rows[0])
    assert db.get('site_health_tokyotosho')['stats']=={'http_attempts':1,'polls':1,'failed_source':1}


def test_restart_keeps_site_cooldown_without_spending_request(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.side_effect=NetworkError('outage',522)
    assert not Sources(db,web).rss(rows[0])
    assert not Sources(db,web).rss(rows[1]) and web.fetch.call_count==1
    assert fresh(db,rows[1])['failures']==0


def test_redirect_waits_then_resumes_without_repeating_first_hop(setup,clock):
    db,rows=tokyo_sources(setup);web=Web()
    redirect=Mock(is_redirect=True,headers={'Location':'https://www.tokyotosho.se/rss.php?terms=Example'},content=b'')
    final=response(rss(kind='tokyotosho'));final.is_redirect=False;final.ok=True
    web.session.request=Mock(side_effect=[redirect,final])
    assert not Sources(db,web).rss(rows[0])
    assert web.session.request.call_count==1 and fresh(db,rows[0])['failures']==0
    clock.now+=10
    assert Sources(db,web).rss(fresh(db,rows[0]))
    assert web.session.request.call_count==2
    assert web.session.request.call_args.args[1]=='https://www.tokyotosho.se/rss.php?terms=Example'
    assert not db.get('site_health_tokyotosho')['redirects']
    assert db.get('site_health_tokyotosho')['stats']=={'http_attempts':2,'deferred':1,'polls':1,'success':1}


def test_redirect_retry_after_is_not_bypassed_by_force_like_call(setup,clock,monkeypatch):
    monkeypatch.setattr('anime.net.time',clock)
    db,rows=tokyo_sources(setup);web=Web()
    redirect=Mock(is_redirect=True,headers={'Location':'/rss2.php','Retry-After':'120'},content=b'')
    final=response(rss(kind='tokyotosho'));final.is_redirect=False;final.ok=True
    web.session.request=Mock(side_effect=[redirect,final])
    assert not Sources(db,web).rss(rows[0])
    clock.now+=10
    assert not Sources(db,web).rss(fresh(db,rows[0])) and web.session.request.call_count==1
    clock.now+=110
    assert Sources(db,web).rss(fresh(db,rows[0])) and web.session.request.call_count==2


def test_rss_and_detail_share_the_same_request_budget(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.return_value=response(rss(kind='tokyotosho'))
    source=Sources(db,web);assert source.rss(rows[0])
    with pytest.raises(RequestDeferred):source.fetch('https://www.tokyotosho.se/details.php?id=1')
    assert web.fetch.call_count==1
