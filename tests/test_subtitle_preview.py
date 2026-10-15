import json
import urllib.error
import urllib.request

import pytest

from anime.db import Store
from anime.subtitle_preview import PreviewServer, byte_range, digest, verified_target


@pytest.fixture
def preview(tmp_path):
    db=Store(tmp_path/'data'/'test.db')
    db.upsert_subject({'id':42,'name':'Original fixture','eps':3})
    mapping={'confirmed':True,'inventory_checked':True,'season':1,'start':1,'end':3}
    db.execute("UPDATE shows SET selected=1,state='watching',authorized=1,mapping=? WHERE id=42",(json.dumps(mapping),))
    db.execute('INSERT INTO source_policies VALUES(42,?,0)',(json.dumps({'mode':'selected'}),))
    media=tmp_path/'library'/'original.mkv';media.parent.mkdir();media.write_bytes(b'original media fixture')
    file={'index':0,'name':media.name,'size':media.stat().st_size}
    selection={'files':[file],'selected':{'2':file},'ids':[0]}
    db.execute("INSERT INTO tasks(hash,show_id,title,status,selection,save_path) VALUES('fixturehash',42,'[Original] unprocessed fixture','complete',?,?)",(json.dumps(selection),str(media.parent)))
    target=verified_target(db,42,2,'fixturehash')
    root=db.path.parent/'subtitle-studio'/'preview-fixture';root.mkdir(parents=True)
    assets={}
    for name in ('video.mp4','diagnostic.ass','reference.ass','font-sc.otf','font-jp.otf'):
        p=root/name;p.write_bytes(b'original preview fixture bytes');assets[name]=p
    (root/'clip-manifest.json').write_text(json.dumps({'target':target,'video_sha256':digest(assets['video.mp4']),'requested_start_seconds':30}))
    server=PreviewServer(db,target,assets).start()
    yield db,media,assets,server
    server.close()


def request(url,headers=None,method='GET'):
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers=headers or {},method=method)) as r:
            return r.status,dict(r.headers),r.read()
    except urllib.error.HTTPError as e:
        return e.code,dict(e.headers),e.read()


@pytest.mark.parametrize('header,expected',[('bytes=0-2',(0,2,True)),('bytes=2-',(2,9,True)),('bytes=-3',(7,9,True)),('bytes=2-99',(2,9,True)),(None,(0,9,False))])
def test_single_byte_ranges(header,expected):
    assert byte_range(header,10)==expected


@pytest.mark.parametrize('header',['bytes=10-','bytes=3-2','bytes=-0','items=0-3','bytes=0-1,4-5','bytes=nope'])
def test_invalid_ranges_are_rejected(header):
    with pytest.raises(ValueError):byte_range(header,10)


def test_loopback_random_allowlist_and_range(preview):
    db,media,assets,server=preview
    before={name:db.rows(f'SELECT * FROM {name}') for name in ('episodes','watches','tasks')}
    assert server.http.server_address[0]=='127.0.0.1'
    status,headers,data=request(server.url)
    assert status==200 and b'__REFERENCE_OFFSET__' not in data and b'actual_listening:false' in data
    status,headers,data=request(server.url+'video.mp4',{'Range':'bytes=1-4'})
    assert status==206 and data==assets['video.mp4'].read_bytes()[1:5]
    assert headers['Content-Range'].startswith('bytes 1-4/')
    assert request(server.url+'video.mp4',{'Range':'bytes=999-1000'})[0]==416
    assert request(server.url+'video.mp4',method='HEAD')[2]==b''
    assert request(server.url+'../../test.db')[0]==404
    assert request(server.url+'video.mp4?unapproved=1')[0]==404
    assert request(server.url,{'Host':'evil.invalid'})[0]==403
    assert request(server.url,{'Sec-Fetch-Site':'cross-site'})[0]==403
    plain=f'http://127.0.0.1:{server.http.server_port}/'
    assert request(plain)[0]==404
    assert all(db.rows(f'SELECT * FROM {name}')==rows for name,rows in before.items())


@pytest.mark.parametrize('failure',['state','cleanup','bounds','blacklist','media_changed'])
def test_every_request_rechecks_real_host_gates(preview,failure):
    db,media,assets,server=preview
    if failure=='state':db.execute("UPDATE shows SET state='wish' WHERE id=42")
    if failure=='cleanup':db.execute('UPDATE shows SET cleanup_hold=1 WHERE id=42')
    if failure=='bounds':
        mapping=db.mapping(42);mapping['end']=1;db.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps(mapping),))
    if failure=='blacklist':db.set('source_publisher_blacklist',[{'publisher':'Original','aliases':['Original'],'reason':'synthetic fixture'}])
    if failure=='media_changed':media.write_bytes(b'changed original fixture')
    assert request(server.url+'video.mp4')[0]==403


def test_asset_change_and_outside_asset_fail_closed(preview,tmp_path):
    db,media,assets,server=preview
    assets['diagnostic.ass'].write_bytes(b'changed asset')
    assert request(server.url+'diagnostic.ass')[0]==409
    outside=tmp_path/'unrelated.txt';outside.write_text('unrelated private file')
    with pytest.raises(ValueError,match='allowlisted'):
        PreviewServer(db,server.target,{**assets,'font-sc.otf':outside})
    assert server.manifest()['actual_listening'] is False and server.manifest()['accepted'] is False
