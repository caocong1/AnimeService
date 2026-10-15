"""Original synthetic fixtures; no production DB/media or real acceptance claims."""
import json,time,hashlib
from pathlib import Path
import pytest
from test_reliability import setup
from test_subtitle_studio import ready
from anime.subtitle_studio import archive_for,lab_core,authored
from anime.subtitle_targets import head,retained_item,media_id,default_binding


def evidence(root,name,data):
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return {'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}


def fixture(setup,monkeypatch):
    db,q,e,r,c=ready(setup);core=lab_core();archive=archive_for(db)
    old=db.path.parent/'old.mkv';old.write_bytes(b'original transitional fixture')
    new=db.path.parent/'sample'/'Example S01E02.mkv';new.parent.mkdir();new.write_bytes(b'new clean synthetic target')
    h='a'*40;selected={'index':0,'name':new.name,'size':new.stat().st_size}
    selection={'selected':{'2':selected},'ids':[0],'files':[selected]}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path,purpose) VALUES(?,42,'[Group] Example S01E02 [1080p][Multi-Subs]','complete',?,?,'subtitle_sample')",(h,json.dumps(selection),str(new.parent)))
    db.execute("INSERT INTO subtitle_samples(candidate_id,show_id,episode,family,hash,state,path,size) VALUES(987,42,2,'fixture',?,'ready',?,?)",(h,str(new),new.stat().st_size))
    db.execute("INSERT INTO episodes(show_id,episode,path,status,size) VALUES(42,2,?,'complete',?)",(str(old),old.stat().st_size))
    target={'path':str(new),'size':new.stat().st_size,'fingerprint':core.fingerprint(new)}
    zh=db.path.parent/'base.vtt';zh.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n明天再见。\n',encoding='utf-8')
    base=archive.add_base(42,1,2,zh,{'language':'chi','source_key':'original-fixture','media_fingerprint':target['fingerprint'],'release':'[Group] Example S01E02'})
    review={'reviewer':'synthetic state-machine fixture','target_fingerprint':target['fingerprint']}
    for k in ('identity','clean','fidelity','content_audio'):review[k]={'status':'verified','evidence':'Original fixture only; not real listening or cleanliness evidence'}
    review['clean']['method']='soft_disabled_continuous';review['content_audio']['actual_listening']=True
    p={'fingerprint':target['fingerprint'],'base_id':base,'expected_target_id':None,'base_review':evidence(e.subtitle_studio.root,'fixture-review.json',review)}
    monkeypatch.setattr(e.subtitle_studio,'media',lambda:[{'show_id':42,'episode':2,'hash':h,**target}])
    return db,q,e,archive,old,new,p,target


def test_freeze_preserves_episode_watch_and_old_workbook(setup,monkeypatch):
    db,q,e,a,old,new,p,target=fixture(setup,monkeypatch)
    original_base=a.add_base(42,1,2,Path(db.path.parent/'base.vtt'),{'language':'chi','source_key':'transition-fixture','media_fingerprint':lab_core().fingerprint(old),'release':'[Group] Example S01E02'})
    original_work=a.workbook(42,1,2,original_base,{'path':str(old),'size':old.stat().st_size,'fingerprint':lab_core().fingerprint(old)})
    original_bytes=original_work.read_bytes()
    before=db.one('SELECT * FROM episodes');db.execute('INSERT INTO watches(show_id,episode,finished,position) VALUES(42,2,0,4)')
    watch=db.one('SELECT * FROM watches')
    result=e.subtitle_studio.production_targets.freeze(42,2,p)
    assert not result['published'] and result['target_id']==head(db,42,2)['id']
    assert db.one('SELECT * FROM episodes')==before and db.one('SELECT * FROM watches')==watch
    book=json.loads(Path(head(db,42,2)['payload']['workbook']).read_bytes())
    assert book['qa']=={} and book['cues'][0]['status']=='pending' and book['target']==target
    e.subtitle_studio.refresh_job(42,2)
    assert json.loads(db.one('SELECT payload FROM subtitle_studio_jobs')['payload'])['target']==target
    assert old.exists() and q.adds==0
    assert original_work.read_bytes()==original_bytes


@pytest.mark.parametrize('failure',['unknown_media','stale_review','no_listening','wish','wrong_manifest','stale_version'])
def test_freeze_rejects_unproved_or_out_of_scope_target(setup,monkeypatch,failure):
    db,q,e,a,old,new,p,target=fixture(setup,monkeypatch)
    if failure=='unknown_media':p['fingerprint']='b'*64
    if failure=='stale_review':Path(p['base_review']['path']).write_text('{}')
    if failure=='no_listening':
        data=json.loads(Path(p['base_review']['path']).read_bytes());data['content_audio']['actual_listening']=False
        p['base_review']=evidence(e.subtitle_studio.root,'no-listen.json',data)
    if failure=='wish':
        from anime.viewing_workflow import POLICY,KEY
        db.set(KEY,POLICY);db.execute("UPDATE shows SET state='wish',auto_download=1 WHERE id=42")
    if failure=='wrong_manifest':db.execute('UPDATE tasks SET save_path=?',(str(old.parent),))
    if failure=='stale_version':p['expected_target_id']='stale'
    before=db.one('SELECT * FROM episodes')
    with pytest.raises(ValueError):e.subtitle_studio.production_targets.freeze(42,2,p)
    assert head(db,42,2) is None and db.one('SELECT * FROM episodes')==before and q.adds==0


def publication_fixture(setup,monkeypatch):
    db,q,e,a,old,new,p,target=fixture(setup,monkeypatch)
    frozen=e.subtitle_studio.production_targets.freeze(42,2,p)
    work=Path(head(db,42,2)['payload']['workbook']);book=json.loads(work.read_bytes())
    directory=a.root/'builds'/'fixture';directory.mkdir(parents=True)
    decision=directory/'decisions.json';decision.write_bytes(work.read_bytes())
    manifest={'id':'fixture','files':{'decisions.json':{'path':str(decision),'sha256':lab_core().digest(decision.read_bytes())}},'target':target}
    monkeypatch.setattr(type(a),'selected',lambda self,*args:manifest)
    player=evidence(e.subtitle_studio.root,'fixture-player.json',{'status':'verified','build_id':'fixture','target_fingerprint':target['fingerprint'],'reviewer':'original fixture','evidence':'Synthetic player state-machine only','actual_playback':True})
    idle=evidence(e.subtitle_studio.root,'fixture-idle.json',{'show_id':42,'episode':2,'status':'verified','web_idle':True,'external_player_idle':True,'reviewer':'original fixture','evidence':'Synthetic idle state-machine','observed_at':time.time()})
    return db,q,e,a,old,new,{'target_id':frozen['target_id'],'build_id':'fixture','player_review':player,'idle_review':idle},target


def test_publish_waits_for_paused_session_then_retains_old_url(setup,monkeypatch):
    db,q,e,a,old,new,p,target=publication_fixture(setup,monkeypatch)
    original=db.one('SELECT * FROM episodes')
    e.subtitle_studio.production_targets.lease({'show_id':42,'episode':2,'id':media_id(old)},'fixture_session_123456')
    assert e.subtitle_studio.production_targets.publish(42,2,p)['state']=='waiting_idle'
    assert db.one('SELECT * FROM episodes')==original and not db.rows('SELECT * FROM subtitle_publications')
    e.subtitle_studio.production_targets.lease({'show_id':42,'episode':2,'id':media_id(old)},'fixture_session_123456',True)
    result=e.subtitle_studio.production_targets.publish(42,2,p)
    assert result['activated'] and db.one('SELECT path FROM episodes')['path']==str(new)
    assert retained_item(db,media_id(old))['path']==str(old) and old.exists()
    assert not db.rows('SELECT * FROM watches') and q.adds==0
    assert e.subtitle_studio.production_targets.publish(42,2,p)['activated']
    assert len(db.rows('SELECT * FROM subtitle_publications'))==1


def test_publish_denies_missing_build_new_edit_and_unproved_idle(setup,monkeypatch):
    db,q,e,a,old,new,p,target=publication_fixture(setup,monkeypatch)
    before=db.one('SELECT * FROM episodes')
    bad={**p,'build_id':'missing'}
    with pytest.raises(ValueError,match='成品'):e.subtitle_studio.production_targets.publish(42,2,bad)
    bad={**p,'idle_review':evidence(e.subtitle_studio.root,'not-idle.json',{'status':'verified'})}
    with pytest.raises(ValueError,match='空闲'):e.subtitle_studio.production_targets.publish(42,2,bad)
    work=Path(head(db,42,2)['payload']['workbook']);book=json.loads(work.read_bytes());book['context']['new_edit']='new';work.write_text(json.dumps(book,ensure_ascii=False),encoding='utf-8')
    with pytest.raises(ValueError,match='工作稿'):e.subtitle_studio.production_targets.publish(42,2,p)
    assert db.one('SELECT * FROM episodes')==before and q.adds==0


def test_core_acceptance_does_not_activate_frozen_research_media(setup,monkeypatch):
    db,q,e,a,old,new,p,target=fixture(setup,monkeypatch)
    e.subtitle_studio.production_targets.freeze(42,2,p)
    assert default_binding(db,{**target,'show_id':42,'episode':2})==(True,None)
    monkeypatch.setattr(type(a),'selected',lambda self,*args:pytest.fail('Research selection is not a published viewing default'))
    assert authored(db,{**target,'show_id':42,'episode':2}) is None


def test_blacklist_after_freeze_blocks_publication_and_retained_urls(setup,monkeypatch):
    db,q,e,a,old,new,p,target=publication_fixture(setup,monkeypatch)
    db.set('source_publisher_blacklist',[{'publisher':'Group','aliases':['Group'],'reason':'synthetic advertisement fixture'}])
    before=db.one('SELECT * FROM episodes')
    with pytest.raises(ValueError):e.subtitle_studio.production_targets.publish(42,2,p)
    assert db.one('SELECT * FROM episodes')==before and q.adds==0


def test_changed_frozen_media_never_retargets_or_overwrites_workbook(setup,monkeypatch):
    db,q,e,a,old,new,p,target=fixture(setup,monkeypatch)
    e.subtitle_studio.production_targets.freeze(42,2,p)
    record=head(db,42,2);work=Path(record['payload']['workbook']);original=work.read_bytes()
    new.write_bytes(b'changed synthetic movie')
    with pytest.raises(ValueError,match='冻结媒体'):e.subtitle_studio.refresh_job(42,2)
    assert head(db,42,2)==record and work.read_bytes()==original


def test_api_session_csrf_scope_and_private_target_responses(setup,monkeypatch):
    from fastapi.testclient import TestClient
    from anime.app import create_app
    db,q,e,a,old,new,p,target=fixture(setup,monkeypatch)
    app=create_app(db,e,False)
    with TestClient(app,base_url='http://127.0.0.1:4871') as client:
        token=client.get('/api/bootstrap').json()['token'];headers={'X-Anime-Token':token}
        url='/api/subtitle-studio/42/2/production-target'
        assert client.post(url,json=p).status_code==403
        assert client.post(url,json=p,headers={**headers,'Origin':'https://example.invalid'}).status_code==403
        result=client.post(url,json=p,headers=headers);assert result.status_code==200
        data=client.get(url).json();assert set(data)=={'target_id','fingerprint'} and 'path' not in str(data)
        lease='/api/web/media/'+media_id(old)+'/viewing-session'
        assert client.get('/api/web/media/'+media_id(old)).json()['viewing_session_supported'] is True
        assert client.post(lease,json={'session':'original_fixture_session'},headers=headers).status_code==200
        assert e.subtitle_studio.production_targets.busy(42,2)
        assert client.post(lease,json={'session':'original_fixture_session','closed':True},headers=headers).status_code==200
        assert not e.subtitle_studio.production_targets.busy(42,2)
        assert q.adds==0


def test_published_build_hash_and_font_assets_are_rechecked(setup,monkeypatch):
    from anime.subtitle_targets import accepted_build
    db,q,e,a,old,new,p,target=fixture(setup,monkeypatch);core=lab_core()
    directory=a.root/'builds'/'originalfixture';directory.mkdir(parents=True)
    output=directory/'bilingual.ass';output.write_bytes(b'original fixture subtitles')
    fonts={}
    for lang in ('zh','ja'):
        font=directory/'fonts'/(lang+'.fixture');font.parent.mkdir(exist_ok=True);font.write_bytes(('original '+lang).encode())
        fonts[lang]={'path':str(font),'sha256':core.digest(font.read_bytes())}
    core.write_json(directory/'acceptance.json',{'render':{'font_assets':fonts}})
    manifest={'id':'originalfixture','renderer_revision':core.RENDERER_REVISION,'files':{'bilingual.ass':{'path':str(output),'sha256':core.digest(output.read_bytes())}},'target':target}
    with a.connect() as c:c.execute('INSERT INTO builds VALUES(?,?,?,?,?,?,?)',('originalfixture',core.episode_key(42,1,2),target['fingerprint'],'fixture',json.dumps(manifest),1,time.time()))
    assert accepted_build(a,core,'originalfixture',target)['id']=='originalfixture'
    output.write_bytes(b'changed output');assert accepted_build(a,core,'originalfixture',target) is None
    output.write_bytes(b'original fixture subtitles');Path(fonts['ja']['path']).write_bytes(b'changed font')
    assert accepted_build(a,core,'originalfixture',target) is None
