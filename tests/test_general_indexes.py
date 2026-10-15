import base64,json
from unittest.mock import Mock
import pytest
from test_reliability import setup,torrent
from test_native_indexes import cohort,rss
from anime.sources import Sources,parse_rss
from anime.native_indexes import metadata,candidate_mapping
from anime.general_indexes import (register_general_sources,magnet_hash,public_magnet,
    parse_1337x,search_1337x,resolve_torrent)
from anime.net import NetworkError,validate_url
from anime.releases import bdecode,Rejected,select_files


def ready(setup):
    db,q,e,r,c=cohort(setup);db.set('general_indexes_enabled',True);db.set('downloader_preference','qbit')
    m=json.loads(db.show(42)['metadata']);m['infobox']=[{'key':'在线播放平台','value':'Netflix'}]
    db.execute('UPDATE shows SET metadata=? WHERE id=42',(json.dumps(m),))
    register_general_sources(db,42)
    return db,q,e


def bt_rss(title='[Group] Example S01E01 [1080p NF WEB-DL][MultiSub]',h=None):
    h=h or bdecode(torrent())[0]
    return ('<rss version="2.0"><channel><title>BT4G</title><item><title>'+title+'</title>'
            '<link>magnet:?xt=urn:btih:'+h+'&amp;tr=https://tracker.invalid/?token=private</link>'
            '<guid>https://bt4gprx.com/magnet/opaqueID</guid><description>Movie</description>'
            '<pubDate>Sun, 11 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>').encode()


def response(data):return Mock(content=data,text=data.decode(errors='replace'))


def row(title='Example S01E01 [MultiSub]',category='28'):
    return ('<table class="table-list"><tr><td class="coll-1"><a href="/sub/'+category+'/0/">Anime</a>'
            '<a href="/torrent/123/Example/">'+title+'</a></td><td class="coll-2">12</td></tr></table>').encode()


def test_general_registration_requires_streaming_evidence_and_keeps_personal_state(setup):
    db,q,e,r,c=cohort(setup);db.set('general_indexes_enabled',True)
    assert register_general_sources(db,42)==0
    db.execute("INSERT INTO candidates(show_id,title,url) VALUES(42,'[Group] Example S01E01 AMZN MultiSub','magnet:hint')")
    before=db.show(42);assert register_general_sources(db,42)==2
    assert db.show(42)==before and q.adds==0
    db.execute("UPDATE sources SET enabled=0,show_id=999 WHERE kind='bt4g'")
    prior=db.rows('SELECT * FROM sources');assert register_general_sources(db,42)==0
    assert db.rows('SELECT * FROM sources')==prior


@pytest.mark.parametrize('protection',['unselected','paused','completed','cleanup','unauthorized','scope_hold','range','disabled'])
def test_generic_discovery_never_expands_scope(setup,protection):
    db,q,e=ready(setup);db.execute("DELETE FROM sources WHERE kind IN ('bt4g','1337x')")
    if protection=='unselected':db.execute('UPDATE shows SET selected=0 WHERE id=42')
    elif protection in ('paused','completed'):db.execute('UPDATE shows SET state=? WHERE id=42',(protection,))
    elif protection=='cleanup':db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    elif protection=='unauthorized':db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    elif protection=='scope_hold':e.source_review.save(42,{'mode':'scope_hold'})
    elif protection=='disabled':db.set('general_indexes_enabled',False)
    else:
        m=db.mapping(42);m['start']=2;db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(m),))
    assert register_general_sources(db,42)==0 and q.adds==0


def test_bt4g_rss_preserves_hash_and_strips_tracker_secrets():
    c=parse_rss(bt_rss(),'bt4g')[0]
    assert c['url']=='magnet:?xt=urn:btih:'+bdecode(torrent())[0]
    assert c['detail']=='https://bt4gprx.com/magnet/opaqueID'
    assert metadata(c)['media_mode']=='soft' and metadata(c)['expected_hash']==magnet_hash(c['url'])
    assert c['published'] and 'private' not in json.dumps(c)
    validate_url('https://bt4gprx.com/search?q=Example&page=rss')
    with pytest.raises(ValueError):validate_url('https://bt4gprx.com.attacker.invalid/search')


def test_base32_and_conflicting_magnets():
    h=bdecode(torrent())[0];b32=base64.b32encode(bytes.fromhex(h)).decode()
    assert public_magnet('magnet:?xt=urn:btih:'+b32)=='magnet:?xt=urn:btih:'+h
    with pytest.raises(Rejected):magnet_hash('magnet:?xt=urn:btih:'+h+'&xt=urn:btih:'+'a'*40)


def test_bt4g_unknown_container_is_rejected_unless_same_hash_has_clean_index_evidence(setup):
    db,q,e=ready(setup);source=db.one("SELECT * FROM sources WHERE kind='bt4g'");s=Sources(db)
    items=parse_rss(bt_rss('[NanakoRaws] Example S01E01.mkv'),'bt4g')
    assert s.remember(source,items)==[]
    db.execute("INSERT INTO sources(show_id,kind,url) VALUES(42,'nyaa','https://nyaa.si/?page=rss&q=Example')")
    nyaa=db.one("SELECT * FROM sources WHERE kind='nyaa'")
    s.remember(nyaa,parse_rss(rss('[NanakoRaws] Example S01E01.mkv'),'nyaa'))
    accepted=s.remember(source,parse_rss(bt_rss('[NanakoRaws] Example S01E01.mkv'),'bt4g'))
    assert len(accepted)==1 and metadata(accepted[0])['mode_basis']['infohash']==bdecode(torrent())[0]
    assert q.adds==0


def test_generic_hard_advertising_ai_wrong_season_and_bundle_never_become_candidates(setup):
    db,q,e=ready(setup);source=db.one("SELECT * FROM sources WHERE kind='bt4g'")
    db.set('source_fidelity','original');db.set('source_publisher_blacklist',[{'publisher':'Skymoon-Raws','aliases':['Skymoon-Raws','Skymoon'],'reason':'ad'}])
    for title in ('Example S01E01 [Hardsub]','Example S02E01 [MultiSub]',
                  'Example S01E01 [MultiSub]-Skymoon-Raws','Example S01E01 [MultiSub] AI upscale',
                  'Example S01E01-E12 [MultiSub]'):
        assert Sources(db).remember(source,parse_rss(bt_rss(title),'bt4g'))==[]
    assert not db.rows("SELECT * FROM candidates WHERE source_id=?",(source['id'],)) and q.adds==0


def test_1337x_search_and_detail_provide_declared_subtitles_not_container_guess(setup):
    db,q,e=ready(setup);source=db.one("SELECT * FROM sources WHERE kind='1337x'")
    h=bdecode(torrent())[0];detail=('<div id="description">Softsubs ASS</div><a href="magnet:?xt=urn:btih:'+h+'">Magnet</a>').encode()
    web=Mock();web.fetch.side_effect=[response(row('Example S01E01 1080p NF WEB-DL')),response(detail)]
    items=search_1337x(db,web,source)
    assert len(items)==1 and metadata(items[0])['media_mode']=='soft' and metadata(items[0])['expected_hash']==h
    assert web.fetch.call_count==2 and q.adds==0
    assert metadata(parse_1337x(row('Example S01E01','81'),source['url'])[0])['media_mode']=='raw'
    with pytest.raises(NetworkError):parse_1337x(b'<title>Just a moment...</title>',source['url'])


def test_1337x_hard_description_overrides_raw_category_and_checks_scope_before_detail(setup):
    db,q,e=ready(setup);source=db.one("SELECT * FROM sources WHERE kind='1337x'");h=bdecode(torrent())[0]
    web=Mock();web.fetch.side_effect=[response(row(category='81')),response(('<div id="description">Hardsub</div><a href="magnet:?xt=urn:btih:'+h+'">Magnet</a>').encode())]
    assert search_1337x(db,web,source)==[]
    db.execute('UPDATE shows SET selected=0 WHERE id=42');web=Mock();web.fetch.return_value=response(row())
    assert search_1337x(db,web,source)==[] and web.fetch.call_count==1


def test_site_outage_uses_shared_backoff_instead_of_stalling_all_series(setup):
    db,q,e=ready(setup);source=db.one("SELECT * FROM sources WHERE kind='1337x'");web=Mock();web.fetch.side_effect=NetworkError('1337x.to HTTP 403')
    assert not Sources(db,web).rss(source)
    second={**source,'url':source['url'].replace('Example','Second')}
    assert not Sources(db,web).rss(second) and web.fetch.call_count==1
    assert not db.rows('SELECT * FROM tasks') and q.adds==0


def test_hash_lookup_resolves_actual_torrent_and_rejects_spoofed_manifest(setup):
    db,q,e=ready(setup);c=parse_rss(bt_rss(),'bt4g')[0];c['show_id']=42
    web=Mock();web.fetch.side_effect=[response(rss()),response(torrent())]
    assert resolve_torrent(db,web,c)==torrent() and web.fetch.call_count==2
    web.fetch.side_effect=[response(rss()),response(torrent((2,)))]
    with pytest.raises(Rejected):resolve_torrent(db,web,c)
    assert q.adds==0


def test_verified_cached_torrent_deduplicates_both_indexes_and_keeps_file_season_gate(setup):
    db,q,e=ready(setup);web=Mock();web.fetch.return_value=response(torrent());s=Sources(db,web);h=bdecode(torrent())[0]
    e.cache.joinpath(h+'.torrent').write_bytes(torrent())
    db.execute("INSERT INTO sources(show_id,kind,url) VALUES(42,'nyaa','https://nyaa.si/?page=rss&q=Example')")
    s.remember(db.one("SELECT * FROM sources WHERE kind='nyaa'"),parse_rss(rss('[Group] Example S01E01 [MultiSub]'),'nyaa'))
    s.remember(db.one("SELECT * FROM sources WHERE kind='bt4g'"),parse_rss(bt_rss(),'bt4g'))
    e.sources=s;e.source_review.start(42)
    assert len(db.rows('SELECT * FROM tasks'))==1 and q.adds==1
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')
    m=candidate_mapping({'aliases':['Example'],'season':2},{'source_metadata':{'index':'bt4g'}})
    with pytest.raises(Rejected):select_files([{'index':0,'name':'Example - 01.mkv','size':100}],m,[1],[1])


def test_missing_torrent_retains_candidate_without_downloader_add(setup):
    db,q,e=ready(setup);s=Sources(db,Mock());source=db.one("SELECT * FROM sources WHERE kind='bt4g'")
    s.remember(source,parse_rss(bt_rss(),'bt4g'));c=db.one('SELECT * FROM candidates WHERE source_id=?',(source['id'],))
    s.web.fetch.side_effect=NetworkError('unavailable')
    with pytest.raises(NetworkError,match='未投递'):s.torrent(c)
    assert db.one('SELECT id FROM candidates WHERE id=?',(c['id'],)) and q.adds==0


def test_scope_change_during_rss_request_prevents_candidate_insert(setup):
    db,q,e=ready(setup);source=db.one("SELECT * FROM sources WHERE kind='bt4g'");web=Mock()
    def fetch(url):
        db.execute('UPDATE shows SET selected=0 WHERE id=42');return response(bt_rss())
    web.fetch.side_effect=fetch;Sources(db,web).rss(source)
    assert not db.rows('SELECT * FROM candidates WHERE source_id=?',(source['id'],)) and q.adds==0
