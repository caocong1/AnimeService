"""Site-level health for RSS sources: alias hosts, shared backoff, request spacing, page detection, event flood control."""
from unittest.mock import Mock
import pytest
from test_reliability import setup
from test_native_indexes import cohort,rss
from anime import sources as sources_module
from anime.sources import Sources,site_of,site_backoff_seconds,SITE_HOSTS
from anime.net import NetworkError,validate_url
from anime.native_indexes import checked_download_url


class Clock:
    def __init__(self,now=1_800_000_000.0):self.now=now
    def time(self):return self.now


def response(content,content_type='application/rss+xml; charset=utf-8'):
    r=Mock();r.content=content;r.headers={'Content-Type':content_type};return r


def tokyo_sources(setup):
    db,q,e,r,c=cohort(setup);e.source_review.discover_sources()
    rows=db.rows("SELECT * FROM sources WHERE kind='tokyotosho' ORDER BY id")
    assert len(rows)==2 and all(site_of(s['url'])=='tokyotosho' for s in rows)
    return db,rows


def fresh(db,source):return db.one('SELECT * FROM sources WHERE id=?',(source['id'],))


@pytest.fixture
def clock(monkeypatch):
    c=Clock();monkeypatch.setattr(sources_module,'time',c);return c


def test_alias_hosts_validate_and_unrelated_mirrors_do_not():
    for host in SITE_HOSTS['tokyotosho']:validate_url(f'https://{host}/rss.php?filter=7&terms=Example')
    with pytest.raises(ValueError):validate_url('https://tokyotosho.org/rss.php?filter=7')
    assert site_of('https://tokyo-tosho.net/x')=='tokyotosho' and site_of('https://nyaa.si/x')=='nyaa.si'
    assert checked_download_url('http://www.tokyotosho.se/details.php?id=1')=='https://www.tokyotosho.se/details.php?id=1'
    assert [site_backoff_seconds(n) for n in (0,1,2,3,7,8,50)]==[60,60,120,240,3600,3600,3600]


def test_failed_host_rotates_to_next_alias_and_source_url_stays_canonical(setup,clock):
    db,rows=tokyo_sources(setup);first=rows[0];web=Mock()
    def fetch(url,**kw):
        if 'www.tokyotosho.info' in url:raise NetworkError('www.tokyotosho.info HTTP 503',503)
        return response(rss(kind='tokyotosho'))
    web.fetch.side_effect=fetch
    assert not Sources(db,web).rss(first)
    state=db.get('site_health_tokyotosho')
    assert state['failures']==1 and state['host']=='www.tokyotosho.se' and 'HTTP 503' in state['error']
    assert fresh(db,first)['url']==first['url'] and fresh(db,first)['next_attempt']==state['next_attempt']
    clock.now=state['next_attempt']+1
    assert Sources(db,web).rss(fresh(db,first))
    assert web.fetch.call_args_list[-1].args[0].startswith('https://www.tokyotosho.se/rss.php?')
    state=db.get('site_health_tokyotosho')
    assert state['failures']==0 and state['error']=='' and state['host']=='www.tokyotosho.se'
    assert db.one('SELECT detail FROM candidates WHERE source_id=?',(first['id'],))['detail'].startswith('https://www.tokyotosho.info/details.php')


def test_site_backoff_is_shared_and_spends_no_extra_request(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.side_effect=NetworkError('www.tokyotosho.info HTTP 429',429)
    assert not Sources(db,web).rss(rows[0]) and not Sources(db,web).rss(rows[1])
    assert web.fetch.call_count==1
    state=db.get('site_health_tokyotosho')
    assert fresh(db,rows[1])['next_attempt']==state['next_attempt']==clock.now+60
    assert fresh(db,rows[1])['failures']==0  # skipped, not failed


def test_site_backoff_grows_to_one_hour_and_resets_on_success(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.side_effect=NetworkError('ConnectTimeout：请求失败，请检查网络或本机接口')
    deltas=[]
    for _ in range(8):
        assert not Sources(db,web).rss(fresh(db,rows[0]));state=db.get('site_health_tokyotosho')
        deltas.append(state['next_attempt']-clock.now);clock.now=state['next_attempt']+1
    assert deltas==[60,120,240,480,960,1920,3600,3600]
    web.fetch.side_effect=None;web.fetch.return_value=response(rss(kind='tokyotosho'))
    assert Sources(db,web).rss(fresh(db,rows[0]))
    assert db.get('site_health_tokyotosho')['failures']==0 and fresh(db,rows[0])['failures']==0


def test_requests_to_one_site_are_spaced_without_counting_failures(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.return_value=response(rss(kind='tokyotosho'))
    assert Sources(db,web).rss(rows[0])
    assert not Sources(db,web).rss(rows[1]) and web.fetch.call_count==1
    second=fresh(db,rows[1])
    assert second['failures']==0 and second['error']=='' and second['next_attempt']==clock.now+10
    assert not db.rows("SELECT * FROM events WHERE scope LIKE 'source:%'")
    clock.now+=10
    assert Sources(db,web).rss(second) and web.fetch.call_count==2
    db.set('site_min_interval_seconds',{'tokyotosho':0})
    assert Sources(db,web).rss(fresh(db,rows[0])) and web.fetch.call_count==3
    # The primary Mikan feeds keep their unspaced behaviour.
    for i in (1,2):db.execute("INSERT INTO sources(show_id,kind,url) VALUES(42,'rss',?)",(f'https://mikanani.me/RSS/Bangumi?bangumiId=90{i}',))
    web.fetch.return_value=response(rss())
    mikan=db.rows("SELECT * FROM sources WHERE kind='rss'")
    assert Sources(db,web).rss(mikan[0]) and Sources(db,web).rss(mikan[1]) and web.fetch.call_count==5


def test_html_page_is_a_site_block_not_a_feed_parse_failure(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock()
    web.fetch.return_value=response(b'<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>cf-chl</body></html>','text/html; charset=utf-8')
    assert not Sources(db,web).rss(rows[0])
    state=db.get('site_health_tokyotosho')
    assert 'HTML' in fresh(db,rows[0])['error'] and state['failures']==1 and state['host']=='www.tokyotosho.se'
    clock.now=state['next_attempt']+1
    web.fetch.return_value=response(b'<rss version="2.0"><channel><title>t</title></channel></rss>')
    assert Sources(db,web).rss(fresh(db,rows[0])) and fresh(db,rows[0])['latest']=='本次RSS无条目'
    clock.now+=100
    web.fetch.return_value=response(b'garbage that is not a feed at all')  # one malformed feed is not a site outage
    assert not Sources(db,web).rss(fresh(db,rows[1]))
    assert db.get('site_health_tokyotosho')['failures']==0 and fresh(db,rows[1])['failures']==1
    assert fresh(db,rows[1])['next_attempt']<=clock.now+300


def test_repeated_identical_failures_do_not_flood_events(setup,clock):
    db,rows=tokyo_sources(setup);web=Mock();web.fetch.side_effect=NetworkError('www.tokyotosho.info HTTP 503',503)
    for _ in range(30):
        assert not Sources(db,web).rss(fresh(db,rows[0]));clock.now=db.get('site_health_tokyotosho')['next_attempt']+1
    assert fresh(db,rows[0])['failures']==30
    assert len(db.rows('SELECT * FROM events WHERE scope=?',('source:%d'%rows[0]['id'],)))==3
    web.fetch.side_effect=NetworkError('www.tokyotosho.info HTTP 502',502)  # a changed message is reported once
    assert not Sources(db,web).rss(fresh(db,rows[0]))
    assert len(db.rows('SELECT * FROM events WHERE scope=?',('source:%d'%rows[0]['id'],)))==4
