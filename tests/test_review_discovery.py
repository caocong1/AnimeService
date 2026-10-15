import json
from urllib.parse import parse_qs,urlparse
import pytest
from test_reliability import setup
from test_source_review import ready_review


def test_review_adds_dmhy_beside_mikan_without_changing_mapping_or_intent(setup):
    db,q,e,r,c=ready_review(setup)
    db.upsert_subject({'id':42,'name':'Japanese title','name_cn':'Chinese title'})
    m=db.mapping(42);m['aliases']+=['English title']
    db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(m),))
    e.source_review.save(42,{'mode':'sampling'})
    before=db.show(42);e.source_review.discover_sources()
    feeds=db.rows("SELECT * FROM sources WHERE kind='dmhy'")
    assert {parse_qs(urlparse(s['url']).query)['keyword'][0] for s in feeds}=={'Chinese title','Japanese title','Example'}
    assert len(db.rows('SELECT * FROM sources'))==4 and db.show(42)==before
    e.source_review.discover_sources()
    assert len(db.rows('SELECT * FROM sources'))==4
    assert not db.rows('SELECT * FROM tasks') and not db.rows('SELECT * FROM watches') and q.adds==0


@pytest.mark.parametrize('protection',['unselected','dropped','unauthorized','inventory','range','scope_hold','sequel'])
def test_supplemental_search_preserves_review_scope_and_identity_gates(setup,protection):
    db,q,e,r,c=ready_review(setup);e.source_review.save(42,{'mode':'sampling'})
    if protection=='unselected':db.execute('UPDATE shows SET selected=0 WHERE id=42')
    elif protection=='dropped':db.execute("UPDATE shows SET state='dropped' WHERE id=42")
    elif protection=='unauthorized':db.execute('UPDATE shows SET authorized=0 WHERE id=42')
    elif protection=='scope_hold':e.source_review.save(42,{'mode':'scope_hold'})
    else:
        m=db.mapping(42);m.update({'inventory':{'inventory_checked':False},'range':{'start':2},'sequel':{'season':2}}[protection])
        db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(m),))
    before=db.show(42);e.source_review.discover_sources()
    assert not db.rows("SELECT * FROM sources WHERE kind='dmhy'") and db.show(42)==before and q.adds==0


def test_supplemental_search_keeps_disabled_source_and_foreign_binding(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.save(42,{'mode':'sampling'})
    url='https://share.dmhy.org/topics/rss/rss.xml?keyword=Example'
    db.execute("INSERT INTO sources(show_id,kind,url,enabled) VALUES(42,'dmhy',?,0)",(url,))
    before=db.rows('SELECT * FROM sources');e.source_review.discover_sources()
    assert db.rows('SELECT * FROM sources')==before
    db.execute('UPDATE sources SET show_id=999 WHERE kind=\'dmhy\'')
    before=db.rows('SELECT * FROM sources');e.source_review.discover_sources()
    assert db.rows('SELECT * FROM sources')==before and q.adds==0


def test_cross_index_same_family_and_hash_never_adds_second_sample(setup):
    db,q,e,r,c=ready_review(setup);e.source_review.start(42)
    db.execute("INSERT INTO sources(id,show_id,kind,url) VALUES(2,42,'dmhy','https://share.dmhy.org/topics/rss/rss.xml?keyword=Example')")
    setup[3](c['title'],source=2,url='https://dl.dmhy.org/same.torrent')
    before=db.rows('SELECT * FROM tasks');e.source_review.start(42)
    assert db.rows('SELECT * FROM tasks')==before and len(db.rows('SELECT * FROM source_samples'))==1 and q.adds==0
