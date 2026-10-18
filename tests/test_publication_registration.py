"""Real temporary Store + Windows lease; never constructs production components."""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest

from anime.db import Store
from anime.ordinary_issuance import OrdinaryIssuer, canonical, digest
from anime.playback_versions import OrdinaryMediaVersions
from anime.webplayer import WebPlayer
from anime.playback_writers import PlaybackMediaResolver, PlaybackWriters

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows sealed publication')


@pytest.fixture
def registered(tmp_path):
    db=Store(tmp_path/'isolated.db')
    # A synthetic authoritative row, read using the same transaction that records the proof.
    db.execute("INSERT INTO shows(id,title,selected,state,authorized,mapping) VALUES(42,'Synthetic',1,'watching',1,?)",(json.dumps({'season':1}),))
    source=tmp_path/'original.mkv';source.write_bytes(b'synthetic bytes')
    original={'id':digest(str(source).lower().encode())[:32],'path':str(source),'size':source.stat().st_size,
              'authorized':True,'association_digest':digest(repr((42,1,1)).encode()),'input_signature':'a'*64}
    root=tmp_path/'private';root.mkdir()
    issuer=OrdinaryIssuer(root,{'fixture':b's'*32},b'p'*32,lambda _:dict(original),reserve_bytes=0)
    versions=OrdinaryMediaVersions(db, WebPlayer(db))
    def evidence(row=None):
        row=row or original
        payload={'issuance_id':'1'*32,'issued_at':1800000000,'expires_at':1800000300,
                 'publisher':'fixture','source_key':row['id'],'sha256':digest(Path(row['path']).read_bytes()),
                 **{k:row[k] for k in ('size','association_digest','input_signature')}}
        value={'schema':'ordinary-source/v1','key_id':'fixture','payload':payload}
        value['mac']=hmac.digest(b's'*32,b'ordinary-source/v1\0'+canonical(value),'sha256').hex()
        return value
    issuer.wall_clock=lambda:1800000001
    def current(c):
        row=c.execute('SELECT selected,state,authorized,mapping FROM shows WHERE id=42').fetchone()
        season=json.loads(row['mapping']).get('season',1)
        return {**original,'authorized':bool(row['selected'] and row['authorized'] and row['state']=='watching'),
                'association_digest':digest(repr((42,1,season)).encode())}
    return db,versions,issuer,original,evidence,current


def test_same_transaction_idempotent_registration_leaves_legacy_position_unchanged(registered):
    db,versions,issuer,original,evidence,current=registered
    db.execute('INSERT INTO web_progress(media_id,position,duration) VALUES(?,?,?)',(original['id'],123,1000))
    with issuer.publish(evidence()) as lease:
        for _ in range(2):
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                key=versions.register_publication(c,lease,current(c))
                assert key==lease.media_key
                lease.assert_live()
        assert len(db.rows('SELECT * FROM ordinary_media_versions'))==1
    assert db.one('SELECT position FROM web_progress WHERE media_id=?',(original['id'],))['position']==123
    assert not db.one('SELECT * FROM web_progress WHERE media_id=?',(key,))
    assert not db.rows("SELECT name FROM sqlite_master WHERE name IN ('playback_writers','playback_claim_receipts')")


@pytest.mark.parametrize('changed', ['scope','mapping','input'])
def test_current_transaction_facts_reject_moved_binding_without_proof(registered,changed):
    db,versions,issuer,original,evidence,current=registered
    with issuer.publish(evidence()) as lease:
        with pytest.raises(ValueError):
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if changed=='scope':c.execute('UPDATE shows SET authorized=0 WHERE id=42')
                if changed=='mapping':c.execute('UPDATE shows SET mapping=? WHERE id=42',(json.dumps({'season':2}),))
                facts=current(c)
                if changed=='input':facts['input_signature']='b'*64
                versions.register_publication(c,lease,facts)
    assert not db.rows('SELECT * FROM ordinary_media_versions')
    assert db.one('SELECT authorized FROM shows WHERE id=42')['authorized']==1


def test_connection_and_closed_lease_fail_closed(registered,tmp_path):
    db,versions,issuer,original,evidence,current=registered
    other=Store(tmp_path/'other.db')
    OrdinaryMediaVersions(other,None)
    with issuer.publish(evidence()) as lease:
        with db.connect() as c:
            with pytest.raises(ValueError,match='explicit transaction'): versions.register_publication(c,lease,original)
        with other.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            with pytest.raises(ValueError,match='same Store'):versions.register_publication(c,lease,original)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError,match='closed'):versions.register_publication(c,lease,current(c))
    assert not db.rows('SELECT * FROM ordinary_media_versions')
    assert not other.rows('SELECT * FROM ordinary_media_versions')


def test_registration_uses_immutable_proof_and_never_mutable_display_fields(registered):
    db,versions,issuer,original,evidence,current=registered
    with issuer.publish(evidence()) as lease:
        proof=dict(lease.proof())
        with pytest.raises(TypeError):lease.proof()['sha256']='f'*64
        lease.media_key='forged';lease.path=Path('C:/forged');lease.size=1
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            key=versions.register_publication(c,lease,current(c));lease.assert_live()
        assert key==proof['media_key']
    row=db.one('SELECT * FROM ordinary_media_versions WHERE media_key=?',(key,))
    assert row['path_key']==proof['path_key'] and row['sha256']==proof['sha256'] and row['size']==proof['size']


def test_fault_rolls_back_other_transaction_writes_and_registration(registered):
    db,versions,issuer,original,evidence,current=registered
    with issuer.publish(evidence()) as lease:
        with pytest.raises(RuntimeError):
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                versions.register_publication(c,lease,current(c))
                c.execute('UPDATE shows SET title=? WHERE id=42',('uncommitted',))
                raise RuntimeError('synthetic crash before commit')
        assert not db.rows('SELECT * FROM ordinary_media_versions')
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            versions.register_publication(c,lease,current(c));lease.assert_live()
    assert db.one('SELECT title FROM shows WHERE id=42')['title']=='Synthetic'
    assert len(db.rows('SELECT * FROM ordinary_media_versions'))==1


def test_expiry_after_insert_rolls_back_before_commit(registered):
    db,versions,issuer,original,evidence,current=registered
    ticks=[0];issuer.clock=lambda:ticks[0]
    with issuer.publish(evidence()) as lease:
        with pytest.raises(ValueError,match='expired'):
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                versions.register_publication(c,lease,current(c))
                ticks[0]=121
                lease.assert_live()
    assert not db.rows('SELECT * FROM ordinary_media_versions')


def test_existing_conflicting_proof_is_preserved(registered):
    db,versions,issuer,original,evidence,current=registered
    with issuer.publish(evidence()) as lease:
        p=dict(lease.proof())
        db.execute('INSERT INTO ordinary_media_versions VALUES(?,?,?,?,?,?)',
                   (p['media_key'],p['path_key'],p['size'],'f'*64,p['association_digest'],1))
        with pytest.raises(ValueError,match='rebind forbidden'):
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                versions.register_publication(c,lease,current(c))
    assert db.one('SELECT sha256 FROM ordinary_media_versions')['sha256']=='f'*64


def test_two_source_aliases_register_one_real_resolver_progress_row(registered):
    db,versions,issuer,original,evidence,current=registered
    with issuer.publish(evidence()) as lease:
        path=lease.path
        size=lease.proof()['size']
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            key=versions.register_publication(c,lease,current(c))
            c.execute("INSERT INTO episodes(show_id,episode,path,size,hash,status) VALUES(42,1,?,?,?,'complete')",
                      (str(path),size,'a'*40))
            lease.assert_live()
    # A second independently bound source alias has the same content and association.
    other=Path(original['path']).with_name('second-original.mkv');other.write_bytes(b'synthetic bytes')
    original.update(path=str(other),id=digest(str(other).lower().encode())[:32])
    with issuer.publish(evidence()) as lease:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            assert versions.register_publication(c,lease,current(c))==key
            lease.assert_live()
    resolver=PlaybackMediaResolver(versions.player,versions)
    assert resolver(key)==key
    writers=PlaybackWriters(db,resolver,enabled=lambda:True)
    packet={'media_key':key,'session':'synthetic-session-123456','request_id':'synthetic-request-123456',
            'writer_token':'a'*64,'device_id':'Synthetic','mode':'open','expected_generation':0}
    claimed=writers.claim(packet);assert claimed['ok']
    assert writers.update(key,{**packet,'generation':claimed['generation'],'sequence':1,
                               'position':12,'duration':100,'playing':True},action='progress')['ok']
    assert len(db.rows('SELECT * FROM ordinary_media_versions'))==1
    assert len(db.rows('SELECT * FROM web_progress'))==1
    assert db.one('SELECT media_id,position FROM web_progress')['media_id']==key
    assert len(list((issuer.root/'aliases').iterdir()))==2
