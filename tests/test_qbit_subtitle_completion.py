"""Completed-video plus missing sidecar regression, isolated DB and fake qB only."""
import json
from pathlib import Path
import pytest
from test_reliability import setup,encode
from anime.releases import bdecode
from anime.source_review import SourceReview


def package(setup):
    db,q,e,c=setup
    raw=encode({b'info':{b'name':b'Example',b'piece length':16384,b'pieces':b'x'*40,b'files':[
        {b'path':[b'Example S01E01.mkv'],b'length':100},
        {b'path':[b'.pad',b'100'],b'length':100,b'attr':b'p'},
        {b'path':[b'Example S01E01.sc.ass'],b'length':80}]}})
    h,manifest=bdecode(raw,include_padding=True)
    selection={'files':[{k:f[k] for k in ('index','name','size')} for f in manifest],
               'ids':[0,2],'selected':{'1':manifest[0]}}
    root=e.library/'first';temp=db.path.parent/'temporary';e.incomplete=temp/'AnimeService'
    content=temp/'Example';content.mkdir(parents=True)
    for f in (manifest[0],manifest[2]):(temp/f['name']).write_bytes(b'x'*f['size'])
    t={'hash':h,'save_path':str(root),'selection':json.dumps(selection)}
    qt={'state':'stoppedUP','progress':1,'amount_left':0,'download_path':str(temp),'content_path':str(content)}
    files=[dict(manifest[0],index=0,progress=1,priority=1),dict(manifest[2],index=1,progress=1,priority=1)]
    (e.cache/(h+'.torrent')).write_bytes(raw);e.verify_qbit_files(t,files)
    return db,q,e,t,qt,files,temp


def test_complete_selected_sidecar_is_published_once_by_name_despite_padding(setup):
    db,q,e,t,qt,files,temp=package(setup)
    before=db.show(42)
    assert not SourceReview.selected_subtitles_complete(t)
    assert e.reconcile_qbit_subtitles(t,qt,files)
    destination=Path(t['save_path'])/files[1]['name'];source=temp/files[1]['name']
    assert destination.samefile(source) and source.exists()
    assert SourceReview.selected_subtitles_complete(t)
    audit=db.get('qbit_file_alias_'+t['hash']+'_1')
    assert audit['source']==str(source.resolve())
    assert e.reconcile_qbit_subtitles(t,qt,files) and db.get('qbit_file_alias_'+t['hash']+'_1')==audit
    assert db.show(42)==before and not db.rows('SELECT * FROM watches')
    assert q.adds==0 and not q.starts and not q.stops


def test_background_reconciliation_includes_sidecar_before_package_completion(setup):
    db,q,e,t,qt,files,temp=package(setup)
    e.sources.data=(e.cache/(t['hash']+'.torrent')).read_bytes()
    e.ingest(setup[3]())
    stored=db.one('SELECT * FROM tasks');h=stored['hash']
    assert json.loads(stored['selection'])['ids']==[0,2]
    q.fs[h]=files;q.tasks[h].update(qt)
    before=db.show(42);controls=(q.adds,list(q.starts),list(q.stops))
    side=temp/files[1]['name'];body=side.read_bytes();side.unlink()
    e.reconcile()
    assert db.one('SELECT status FROM tasks')['status']=='downloading'
    assert db.one('SELECT status FROM episodes')['status']=='complete'
    side.write_bytes(body);e.reconcile()
    stored=db.one('SELECT * FROM tasks');target=Path(stored['save_path'])/files[1]['name']
    assert stored['status']=='complete' and target.samefile(side)
    assert db.show(42)==before and not db.rows('SELECT * FROM watches')
    assert (q.adds,q.starts,q.stops)==controls


def test_declared_chinese_cannot_publish_first_episode_with_missing_selected_sidecar(setup):
    db,q,e,t,qt,files,temp=package(setup)
    e.sources.data=(e.cache/(t['hash']+'.torrent')).read_bytes();e.ingest(setup[3]())
    stored=db.one('SELECT * FROM tasks');h=stored['hash'];q.fs[h]=files;q.tasks[h].update(qt)
    e.reconcile();db.execute('DELETE FROM episodes')
    db.execute("UPDATE tasks SET purpose='source_sample' WHERE hash=?",(h,))
    cid=db.one('SELECT id FROM candidates')['id']
    p=Path(stored['save_path'])/files[0]['name'];side=Path(stored['save_path'])/files[1]['name'];side.unlink()
    db.execute("INSERT INTO source_samples(candidate_id,show_id,hash,family,path,size,state,evidence,review,updated) VALUES(?,42,?,'test',?,100,'analysis_error','{}','{}',0)",(cid,h,str(p)))
    e.source_review.ensure(42,submission=False);db.set('play_completed_premiere_enabled',True)
    e.source_review.publish_available()
    assert not db.rows('SELECT * FROM episodes')
    assert db.one('SELECT purpose FROM tasks')['purpose']=='source_sample'


@pytest.mark.parametrize('guard',[{'state':'claimed'},{'state':'review'},{'lease':'fixture-only'},{'committing':1}])
def test_protected_queue_never_publishes_sidecars_or_changes_completed_ledger(setup,guard):
    db,q,e,t,qt,files,temp=package(setup)
    e.sources.data=(e.cache/(t['hash']+'.torrent')).read_bytes();e.ingest(setup[3]())
    h=db.one('SELECT hash FROM tasks')['hash'];q.fs[h]=files;q.tasks[h].update(qt)
    db.execute("UPDATE tasks SET status='complete'")
    values={'state':'cancelled','lease':'','committing':0,**guard}
    db.execute('INSERT INTO thunder_queue(hash,state,lease,committing) VALUES(?,?,?,?)',(h,values['state'],values['lease'],values['committing']))
    before=db.one('SELECT * FROM tasks');ep=db.one('SELECT * FROM episodes')
    controls=(q.adds,list(q.starts),list(q.stops));e.reconcile()
    assert db.one('SELECT * FROM tasks')==before and db.one('SELECT * FROM episodes')==ep
    assert not (Path(before['save_path'])/files[1]['name']).exists()
    assert (q.adds,q.starts,q.stops)==controls


@pytest.mark.parametrize('failure',['missing','partial','wrong_size','foreign_temp','foreign_content','missingFiles','existing_other'])
def test_missing_or_unqualified_sidecar_cannot_be_published_or_claimed_complete(setup,failure):
    db,q,e,t,qt,files,temp=package(setup);source=temp/files[1]['name'];destination=Path(t['save_path'])/files[1]['name']
    if failure=='missing':source.unlink()
    elif failure=='partial':files[1]['progress']=.5;qt['progress']=.5;qt['amount_left']=40
    elif failure=='wrong_size':source.write_bytes(b'x')
    elif failure=='foreign_temp':qt['download_path']=str(temp/'other')
    elif failure=='foreign_content':qt['content_path']=str(temp/'other')
    elif failure=='missingFiles':qt['state']='missingFiles'
    else:destination.parent.mkdir(parents=True);destination.write_bytes(b'other')
    assert not e.reconcile_qbit_subtitles(t,qt,files)
    assert not SourceReview.selected_subtitles_complete(t)
    assert not db.get('qbit_file_alias_'+t['hash']+'_1')
    if failure=='existing_other':assert destination.read_bytes()==b'other'
    assert q.adds==0 and not q.starts and not q.stops
