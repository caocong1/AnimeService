import contextlib,hashlib,json,math

import pytest
from anime.board import episode_board
from anime.fidelity import fidelity_blocker
from anime.source_blacklist import KEY,source_blocker
from anime.source_review import family
from test_show_list_performance import setup


def legacy_review(review,sid):
    """Pre-optimization read path as a full-response compatibility oracle."""
    db=review.db
    samples=db.rows('''SELECT s.*,c.title,c.published FROM source_samples s JOIN candidates c
        ON c.id=s.candidate_id WHERE s.show_id=? ORDER BY s.candidate_id''',(sid,))
    for s in samples:
        s['evidence']=json.loads(s['evidence']);s['review']=json.loads(s['review'])
        s['family_info']=family(s['title'])
        task=db.one('SELECT selection FROM tasks WHERE hash=?',(s['hash'],))
        s['source_blocker']=source_blocker(db,s['title'],task['selection'] if task else None)
        s['processing_blocker']=fidelity_blocker(db,s['title'],task['selection'] if task else None)
        s['release_delay_hours']=review.delay(sid,s['published'])
        if s['hash']:s['download']=db.one('SELECT status,error,provider FROM tasks WHERE hash=?',(s['hash'],)) or {}
    recommendations=sorted([s for s in samples if review.reviewed(s) and not s['processing_blocker']],key=lambda s:(
        -s['review']['visual'],review.delay(sid,s['published']) if review.delay(sid,s['published']) is not None else math.inf,
        -s['review']['translation']))
    candidates=review.candidates(sid)
    families={c['family']['key'] for c in candidates};sampled={s['family'] for s in samples}
    return {'policy':review.policy(sid),'candidates':candidates,'samples':samples,
        'sampling_limit':{'max_families':12,'max_gib':20,'available_families':len(families),
            'sampled_families':len(sampled),'remaining_families':len(families-sampled)},
        'recommendation':[s['family'] for s in recommendations],'fidelity':db.get('source_fidelity',''),
        'publisher_blacklist':db.get(KEY,[]),
        'priority':['原片保真','实测画质','发布延迟','字幕翻译'] if db.get('source_fidelity','')=='original' else ['实测画质','发布延迟','字幕翻译'],
        'note':'首集评测需核对实际画面、日语原声/文本及原作术语；未知项不打分。评测副本不计已看、不自动删除。'}


def legacy_detail(db,engine,sid):
    s=db.show(sid)
    for key in ('mapping','tags','metadata'):s[key]=json.loads(s[key])
    episodes=db.rows('''SELECT episodes.*,coalesce(watches.finished,0) finished,watches.method watch_method
        FROM episodes LEFT JOIN watches ON episodes.show_id=watches.show_id AND episodes.episode=watches.episode
        WHERE episodes.show_id=? ORDER BY episode''',(sid,))
    for e in episodes:
        e['media_id']=hashlib.sha256(e['path'].lower().encode()).hexdigest()[:32] if e['path'] else None
        e['progress']=db.one('SELECT position,duration FROM web_progress WHERE media_id=?',(e['media_id'],)) if e['media_id'] else None
    return {'show':s,'episodes':episodes,'watches':db.rows('SELECT * FROM watches WHERE show_id=? ORDER BY episode',(sid,)),
        'sources':db.rows('SELECT * FROM sources WHERE show_id=?',(sid,)),
        'candidates':db.rows('SELECT * FROM candidates WHERE show_id=? ORDER BY id DESC LIMIT 100',(sid,)),
        'gaps':engine.gaps(sid),'board':episode_board(db,engine,s),'airings':db.local_airings(sid),
        'source_review':legacy_review(engine.source_review,sid)}


def seed(db,tmp_path,count):
    db.upsert_subject({'id':1,'name':'Example','total_episodes':count+2})
    db.execute("UPDATE shows SET selected=1,state='watching',authorized=1,auto_download=1,mapping=? WHERE id=1",
        (json.dumps({'aliases':['Example'],'confirmed':True,'inventory_checked':True,'airing_offset':12}),))
    p=tmp_path/'one.mp4';p.write_bytes(b'video')
    with db.connect() as c:
        c.execute("INSERT INTO sources(id,show_id,enabled,url) VALUES(1,1,1,'https://example.test/rss')")
        c.executemany("INSERT INTO episodes(show_id,episode,path,size,status,progress) VALUES(1,?,?,5,'complete',.5)",
            [(n,str(p) if n==1 else str(tmp_path/f'{n}.mp4')) for n in range(1,count+1)])
        c.executemany('INSERT INTO candidates(id,show_id,source_id,title,url,published) VALUES(?,1,1,?,?,?)',
            [(n,f'[Group{n}] Example - 01 [1080p][CHS]',f'https://example.test/{n}',100+n) for n in range(1,count+1)])
        c.executemany("INSERT INTO source_samples(candidate_id,show_id,family,hash,state,review) VALUES(?,1,?,?,'ready',?)",
            [(n,family(f'[Group{n}] Example - 01 [1080p][CHS]')['key'],str(n),json.dumps({
                'visual':80,'translation':75,'chinese_verified':True,'confidence':'medium','evidence':'fixture'})) for n in range(1,count+1)])
        c.execute("INSERT INTO tasks(hash,show_id,status,selection) VALUES('1',1,'complete',?)",
            (json.dumps({'ids':[0],'files':[{'index':0,'name':'Example AI2160p.mkv'}]}),))
        c.execute("INSERT INTO airings VALUES(1,?,'2000-01-01','aired')",(count+13,))
    key=hashlib.sha256(str(p).lower().encode()).hexdigest()[:32]
    db.execute('INSERT INTO web_progress(media_id,position,duration) VALUES(?,100,1000)',(key,))
    db.set('source_fidelity','original')
    return p


@pytest.mark.parametrize('state',['watching','paused','dropped','completed'])
def test_detail_preserves_complete_response_and_inactive_gaps(tmp_path,state):
    db,engine,client=setup(tmp_path);seed(db,tmp_path,5)
    db.execute('UPDATE shows SET state=? WHERE id=1',(state,))
    engine.source_review.save(1,{'airing_epoch':100,'interval_hours':168,'mode':'sampling'})
    expected=legacy_detail(db,engine,1)
    before=db.show(1)
    assert client.get('/api/shows/1').json()==expected
    assert db.show(1)==before and not db.rows('SELECT * FROM watches')
    assert not engine.qb.mock_calls and not engine.sources.mock_calls


@pytest.mark.parametrize('count',[1,40,405])
def test_detail_connections_are_bounded_by_batches_not_samples(tmp_path,monkeypatch,count):
    db,engine,client=setup(tmp_path);seed(db,tmp_path,count)
    opened=[];queries=[];original=db.connect
    @contextlib.contextmanager
    def counted():
        opened.append(1)
        with original() as c:
            c.set_trace_callback(queries.append)
            yield c
    monkeypatch.setattr(db,'connect',counted)
    response=client.get('/api/shows/1')
    assert response.status_code==200
    assert len(response.json()['source_review']['samples'])==count
    assert len(opened)==2
    assert len([q for q in queries if q.startswith('SELECT')])<=12+(count+399)//400
    assert not engine.qb.mock_calls and not engine.sources.mock_calls


def test_detail_next_request_sees_all_mutations(tmp_path):
    db,engine,client=setup(tmp_path);p=seed(db,tmp_path,2)
    first=client.get('/api/shows/1').json()
    assert first['board']['next']['s']=='resume'
    assert first['source_review']['samples'][0]['processing_blocker']
    db.set('source_fidelity','')
    engine.source_review.save(1,{'airing_epoch':100})
    db.execute("UPDATE sources SET enabled=0 WHERE id=1")
    db.execute("UPDATE tasks SET status='error',error='fixture error' WHERE hash='1'")
    db.execute('INSERT INTO watches(show_id,episode,finished) VALUES(1,1,1)')
    db.execute('UPDATE web_progress SET position=200')
    second=client.get('/api/shows/1').json()
    assert second['episodes'][0]['progress']['position']==200 and second['episodes'][0]['finished']==1
    assert second['board']['eps'][0]['s']=='watched'
    assert not second['source_review']['candidates'] and second['source_review']['samples'][0]['processing_blocker']==''
    assert second['source_review']['samples'][0]['download']['status']=='error'
    assert second['source_review']['samples'][0]['release_delay_hours']==round(1/3600,2)
    db.execute('UPDATE watches SET finished=0')
    p.unlink()
    assert client.get('/api/shows/1').json()['board']['eps'][0]['s']=='missing'
    assert client.get('/api/shows/999').status_code==404
