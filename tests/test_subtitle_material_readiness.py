"""Offline subtitle inventory uses verified completions, never file-only claims."""
import json
from pathlib import Path
import pytest
from test_reliability import setup,torrent
from test_subtitle_studio import ready
from anime.subtitle_studio import archive_for


def completed_sample(setup,purpose='subtitle_sample'):
    db,q,e,_,original=ready(setup)
    db.set('downloader_preference','qbit')
    ep=1 if purpose=='source_sample' else 2
    c=original if ep==1 else setup[3]('[Group] Example S01E02 [1080p][CHS]',
                                     url='https://mikanani.me/episode2.torrent')
    e.sources.data=torrent((ep,))
    if ep==1:e.source_review.start(42,[c['id']])
    else:e.subtitle_studio.queue(next(x for x in e.subtitle_studio.candidates(42) if x['id']==c['id']))
    task=db.one('SELECT * FROM tasks')
    f=json.loads(task['selection'])['selected'][str(ep)]
    path=Path(task['save_path'])/f['name'];path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'x'*f['size'])
    q.fs[task['hash']][0]['progress']=1
    q.tasks[task['hash']].update(state='stoppedUP',progress=1,amount_left=0)
    e.reconcile()
    assert db.one('SELECT status FROM tasks')['status']=='complete'
    table='source_samples' if ep==1 else 'subtitle_samples'
    # Reproduce the stale metadata independently from the verified task ledger.
    db.execute(f"UPDATE {table} SET path='',state='queued' WHERE candidate_id=?",(c['id'],))
    q.online=False
    return db,q,e,table,c,path,ep


@pytest.mark.parametrize('purpose',['source_sample','subtitle_sample'])
def test_verified_complete_sample_can_be_archived_while_qbit_is_offline(setup,monkeypatch,purpose):
    db,q,e,table,c,path,ep=completed_sample(setup,purpose)
    before=(db.one('SELECT * FROM tasks'),db.one(f'SELECT * FROM {table}'),db.show(42))
    controls=(q.adds,list(q.starts),list(q.stops))
    item=e.subtitle_studio.media()[0]
    assert Path(item['path'])==path.resolve() and item['episode']==ep
    assert '_derived_purpose' not in item
    zh=db.path.parent/'chi.vtt';ja=db.path.parent/'jpn.vtt'
    zh.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n明天再见。\n',encoding='utf-8')
    ja.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nまた明日。\n',encoding='utf-8')
    monkeypatch.setattr(e.subtitle_studio.sub,'tracks',lambda item:[
        {'index':0,'supported':True,'codec':'subrip','language':'chi'},
        {'index':1,'supported':True,'codec':'subrip','language':'jpn'}])
    monkeypatch.setattr(e.subtitle_studio.sub,'extract',lambda item,index,fmt:zh if index==0 else ja)
    result=e.subtitle_studio.collect(item)
    assert len(result['draft_ids'])==2 and len(archive_for(db).bases(42,1,ep))==2
    assert db.one('SELECT fingerprint FROM subtitle_studio_media')['fingerprint']==item['fingerprint']
    assert (db.one('SELECT * FROM tasks'),db.one(f'SELECT * FROM {table}'),db.show(42))==before
    assert not db.rows('SELECT * FROM episodes') and not db.rows('SELECT * FROM watches')
    assert (q.adds,q.starts,q.stops)==controls


@pytest.mark.parametrize('failure',[
    'uncertain','downloading','review','wrong_task_owner','wrong_purpose','foreign_provider','candidate_owner',
    'candidate_episode','candidate_hash','source_owner','mapping_season','episode_bound',
    'manifest_size','manifest_ids','manifest_episode','manifest_escape','cache_hash',
    'foreign_root','wrong_file_size','missing_file','blacklist','lease','committing','dialog_hold'])
def test_file_presence_cannot_override_manifest_scope_ownership_or_unknown_result(setup,failure):
    db,q,e,table,c,path,ep=completed_sample(setup)
    task=db.one('SELECT * FROM tasks');selection=json.loads(task['selection'])
    if failure in ('uncertain','downloading','review'):
        db.execute('UPDATE tasks SET status=?',(failure,))
    elif failure=='wrong_task_owner':db.execute('UPDATE tasks SET show_id=43')
    elif failure=='wrong_purpose':db.execute("UPDATE tasks SET purpose='source_sample'")
    elif failure=='foreign_provider':db.execute("UPDATE tasks SET provider='external'")
    elif failure=='candidate_owner':db.execute('UPDATE candidates SET show_id=43 WHERE id=?',(c['id'],))
    elif failure=='candidate_episode':db.execute("UPDATE candidates SET title='[Group] Example S01E03 [1080p][CHS]' WHERE id=?",(c['id'],))
    elif failure=='candidate_hash':db.execute('UPDATE candidates SET hash=? WHERE id=?',('b'*40,c['id']))
    elif failure=='source_owner':db.execute('UPDATE sources SET show_id=43')
    elif failure in ('mapping_season','episode_bound'):
        mapping=db.mapping(42);mapping['season' if failure=='mapping_season' else 'end']=2 if failure=='mapping_season' else 1
        db.execute('UPDATE shows SET mapping=?',(json.dumps(mapping),))
    elif failure.startswith('manifest_'):
        if failure=='manifest_size':selection['selected']['2']['size']+=1
        elif failure=='manifest_ids':selection['ids']=[]
        elif failure=='manifest_episode':selection['selected']={'3':selection['selected']['2']}
        else:selection['files'][0]['name']='../foreign.mkv'
        db.execute('UPDATE tasks SET selection=?',(json.dumps(selection),))
    elif failure=='cache_hash':(e.cache/(task['hash']+'.torrent')).write_bytes(torrent((3,)))
    elif failure=='foreign_root':
        foreign=db.path.parent/'outside';other=foreign/selection['selected']['2']['name']
        other.parent.mkdir(parents=True);other.write_bytes(path.read_bytes())
        db.execute('UPDATE tasks SET save_path=?',(str(foreign),))
    elif failure=='wrong_file_size':path.write_bytes(b'partial')
    elif failure=='missing_file':path.unlink()
    elif failure=='blacklist':db.set('source_publisher_blacklist',[{'publisher':'Group','aliases':['Group'],'reason':'Original synthetic advertisement fixture'}])
    elif failure in ('lease','committing'):
        db.execute('INSERT INTO thunder_queue(hash,state,lease,committing) VALUES(?,?,?,?)',
                   (task['hash'],'done','original_fixture_lease' if failure=='lease' else '',int(failure=='committing')))
    else:db.set('thunder_dialog_hold_'+task['hash'],{'unconfirmed':True})
    before=(db.one('SELECT * FROM tasks'),db.one(f'SELECT * FROM {table}'))
    controls=(q.adds,list(q.starts),list(q.stops))
    assert e.subtitle_studio.media()==[]
    assert (db.one('SELECT * FROM tasks'),db.one(f'SELECT * FROM {table}'))==before
    assert (q.adds,q.starts,q.stops)==controls


def test_linked_sample_needs_the_existing_exact_read_only_receipt(setup):
    db,q,e,table,c,path,ep=completed_sample(setup)
    task=db.one('SELECT * FROM tasks');db.execute("UPDATE tasks SET status='linked'")
    assert not e.subtitle_studio.media()
    receipt={'mode':'read_only','hash':task['hash'],'path':str(path),'size':path.stat().st_size}
    db.set('thunder_readonly_'+task['hash'],receipt)
    assert Path(e.subtitle_studio.media()[0]['path'])==path.resolve()
    db.set('thunder_readonly_'+task['hash'],{**receipt,'hash':'c'*40})
    assert not e.subtitle_studio.media()
    assert db.one(f'SELECT path,state FROM {table}')=={'path':'','state':'queued'}
