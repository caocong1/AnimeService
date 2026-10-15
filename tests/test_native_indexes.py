import base64,json,time
from unittest.mock import Mock
import pytest
from test_reliability import setup,torrent
from test_source_review import ready_review
from anime.sources import Sources,parse_rss
from anime.native_indexes import (register_sources,media_mode,candidate_mapping,
    task_clean_blocker,verify_hash)
from anime.net import validate_url
from anime.releases import bdecode,validate_title,select_files,Rejected


def rss(title='[RawGroup] Example S01E01 [1080p]',kind='nyaa',hash=None):
    h=hash or bdecode(torrent())[0]
    if kind=='nyaa':
        category='<nyaa:categoryId>1_4</nyaa:categoryId><nyaa:infoHash>'+h+'</nyaa:infoHash>'
        detail='https://nyaa.si/view/123'
    else:
        category='<category>Raws</category>'
        detail='https://www.tokyotosho.info/details.php?id=123'
    magnet=base64.b32encode(bytes.fromhex(h)).decode()
    return ('<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel><title>Test</title><link>https://nyaa.si/</link><description>Test</description><item>'
        '<title>'+title+'</title><link>https://nyaa.si/download/123.torrent</link><guid>'+detail+'</guid>'+category+
        '<description><![CDATA[<a href="magnet:?xt=urn:btih:'+magnet+'&amp;tr=udp://tracker.invalid">Magnet</a>]]></description>'
        '<pubDate>Sun, 11 Oct 2026 00:00:00 GMT</pubDate></item></channel></rss>').encode()


def cohort(setup):
    db,q,e,r,c=ready_review(setup)
    db.set('native_indexes_enabled',True);db.set('subtitle_studio_enabled',True)
    db.set('subtitle_clean_media_preferred',True)
    e.source_review.save(42,{'mode':'sampling'})
    return db,q,e,r,c


@pytest.mark.parametrize('kind',['nyaa','tokyotosho'])
def test_rss_preserves_detail_download_declared_hash_and_raw_category(kind):
    c=parse_rss(rss(kind=kind),kind)[0]
    assert c['url']=='https://nyaa.si/download/123.torrent'
    assert '/view/' in c['detail'] if kind=='nyaa' else '/details.php' in c['detail']
    assert c['source_metadata']['expected_hash']==bdecode(torrent())[0]
    assert c['source_metadata']['media_mode']=='raw' and c['source_metadata']['declaration_only']
    assert c['published']>0


def test_publisher_name_and_container_do_not_prove_clean_picture():
    assert media_mode({'title':'[Erai-raws] Example [1080p].mkv'})=='unknown'
    assert media_mode({'title':'[RawGroup] Example [Hardsub]','source_metadata':{'media_mode':'raw'}})=='hard'
    assert media_mode({'title':'[Group] Example [Multi-Subs]'})=='soft'
    assert media_mode({'title':'[Group] Example [繁中内嵌]'})=='hard'


@pytest.mark.parametrize('url',['http://nyaa.si/','https://www.nyaa.eu/','https://nyaa.si.attacker.invalid/','https://nyaa.si/?token=secret'])
def test_index_whitelist_does_not_allow_mirrors_credentials_or_http(url):
    with pytest.raises(ValueError):validate_url(url)
    validate_url('https://nyaa.si/?page=rss&c=1_0&q=Example')
    validate_url('https://www.tokyotosho.info/rss.php?filter=7&terms=Example')


def test_index_registration_keeps_mapping_disabled_sources_and_foreign_owner(setup):
    db,q,e,r,c=cohort(setup);m=db.mapping(42);m['season']=3
    db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(m),))
    before=db.show(42);e.source_review.discover_sources()
    feeds=db.rows("SELECT * FROM sources WHERE kind IN ('nyaa','tokyotosho')")
    assert len(feeds)==3 and db.show(42)==before and q.adds==0
    db.execute("UPDATE sources SET enabled=0,show_id=999 WHERE kind='nyaa'")
    before=db.rows('SELECT * FROM sources');register_sources(db,42)
    assert before==db.rows('SELECT * FROM sources')


@pytest.mark.parametrize('protection',['unselected','paused','completed','cleanup','unauthorized','scope_hold','range'])
def test_new_indexes_never_expand_personal_or_episode_authorization(setup,protection):
    db,q,e,r,c=cohort(setup)
    if protection=='unselected':db.execute('UPDATE shows SET selected=0 WHERE id=42')
    elif protection in ('paused','completed'):db.execute('UPDATE shows SET state=? WHERE id=42',(protection,))
    elif protection=='cleanup':db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    elif protection=='unauthorized':db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    elif protection=='scope_hold':e.source_review.save(42,{'mode':'scope_hold'})
    else:
        m=db.mapping(42);m['start']=2;db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(m),))
    before=db.show(42);assert register_sources(db,42)==0
    assert before==db.show(42) and q.adds==0


def test_remember_rejects_wrong_season_hard_advertising_and_processed_sources(setup):
    db,q,e,r,c=cohort(setup);register_sources(db,42)
    source=db.one("SELECT * FROM sources WHERE kind='nyaa'")
    db.set('source_fidelity','original')
    db.set('source_publisher_blacklist',[{'publisher':'Skymoon-Raws','aliases':['Skymoon-Raws','Skymoon'],'reason':'verified ad'}])
    items=[]
    for title in ('[Good] Example S01E01 [1080p]','[Wrong] Example S02E01 [1080p]',
                  '[Hard] Example S01E01 [Hardsub]','[Skymoon-Raws] Example S01E01 [1080p]',
                  '[AI] Example S01E01 [AI upscale]'):
        c=parse_rss(rss(title), 'nyaa')[0];c['url']=c['url'].replace('123',str(len(items)+1));items.append(c)
    Sources(db).remember(source,items)
    assert [c['title'] for c in db.rows('SELECT * FROM candidates WHERE source_id=?',(source['id'],))]==['[Good] Example S01E01 [1080p]']


def test_rss_scope_rechecked_after_network_fetch(setup):
    db,q,e,r,c=cohort(setup);register_sources(db,42)
    source=db.one("SELECT * FROM sources WHERE kind='nyaa'")
    def fetch(url):
        db.execute('UPDATE shows SET selected=0 WHERE id=42');return Mock(content=rss())
    Sources(db,Mock(fetch=fetch)).rss(source)
    assert not db.rows('SELECT 1 FROM candidates WHERE source_id=?',(source['id'],))


def test_declared_infohash_mismatch_is_rejected_before_submission():
    with pytest.raises(Rejected,match='infohash'):verify_hash({'source_metadata':{'expected_hash':'f'*40}},torrent())


def test_sequel_requires_correct_title_and_file_season_with_underscore_variant():
    original={'season':3,'aliases':['Ranma 1/2 (2024)']}
    mapping=candidate_mapping(original,{'source_metadata':{'index':'nyaa'}})
    assert original=={'season':3,'aliases':['Ranma 1/2 (2024)']}
    assert validate_title('[Raw] Ranma 1_2 (2024) S3 - 01 [1080p]',mapping,{},False)==[1]
    with pytest.raises(Rejected):validate_title('[Raw] Ranma 1_2 (2024) - 01 [1080p]',mapping,{},False)
    f={'index':0,'name':'Ranma 1_2 (2024) S3 - 01.mkv','size':100}
    assert select_files([f],mapping,[1],[1])[0][1]==f
    f['name']='Ranma 1_2 (2024) - 01.mkv'
    with pytest.raises(Rejected,match='季号'):select_files([f],mapping,[1],[1])


def test_clean_premiere_from_both_indexes_uses_one_existing_hash(setup):
    db,q,e,r,c=cohort(setup);register_sources(db,42)
    sources=db.rows("SELECT * FROM sources WHERE kind IN ('nyaa','tokyotosho')")
    for source in sources:
        Sources(db).remember(source,parse_rss(rss(kind=source['kind']),source['kind']))
    e.source_review.start(42)
    assert len(db.rows('SELECT * FROM tasks'))==1
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')
    assert q.adds==0


@pytest.mark.parametrize('title',['Example S1-S3 - 01','Example S1 - S3 - 01','Example S01-03 - 01'])
def test_actual_cross_season_bundles_remain_rejected(title):
    with pytest.raises(Rejected,match='跨季'):validate_title(title,{'aliases':['Example'],'season':1},{},False)


def test_clean_episode_two_sample_obeys_regular_qbit_and_uncertainty_gates(setup):
    db,q,e,r,c=cohort(setup);db.set('downloader_preference','qbit')
    candidate=setup[3]('[Native] Example S01E02 [1080p][MultiSub]',url='https://mikanani.me/ep2.torrent')
    e.sources.data=torrent((2,))
    c=next(c for c in e.subtitle_studio.candidates(42) if c['id']==candidate['id'])
    e.subtitle_studio.queue(c);e.subtitle_studio.queue(c)
    assert q.adds==1 and db.one('SELECT purpose FROM tasks')['purpose']=='subtitle_sample'
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')


def test_pending_hard_tasks_block_new_submission_but_existing_uncertain_records_remain(setup):
    db,q,e,r,c=cohort(setup)
    task={'show_id':42,'hash':'a'*40,'title':'[Group] Example S01E01 [CHS][内嵌]',
          'status':'intent','acknowledged':0}
    assert task_clean_blocker(db,task)
    assert not task_clean_blocker(db,{**task,'status':'uncertain'})
    assert not task_clean_blocker(db,{**task,'acknowledged':1})
    assert not e.source_review.permits(c)
    assert q.adds==0


def test_late_qbit_receipt_recovers_sample_without_second_add(setup):
    db,q,e,r,c=cohort(setup);db.set('downloader_preference','qbit');q.ambiguous=True
    candidate=setup[3]('[Native] Example S01E01 [1080p][MultiSub]',url='https://mikanani.me/native.torrent')
    e.source_review.start(42,[candidate['id']])
    assert db.one('SELECT status FROM tasks')['status']=='uncertain'
    assert db.one('SELECT state FROM source_samples')['state']=='error' and q.adds==1
    e.reconcile()
    assert db.one('SELECT status FROM tasks')['status']=='downloading'
    assert db.one('SELECT state,error FROM source_samples')=={'state':'queued','error':''}
    assert q.adds==1 and not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')
