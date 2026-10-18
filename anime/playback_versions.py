"""Opt-in, immutable ordinary-media evidence. No production constructor or registration.

Only a trusted backend verifier may call register with its independently known
SHA-256. No HTTP endpoint learns/rebinds a version from client input. Every v2
resolution rereads all bytes within a bounded budget; stat alone is not evidence.
"""
import hashlib
import math
import os
import re
import stat
import time
from pathlib import Path


def fingerprint(value):
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)


def file_identity(value):
    # Windows stat().ctime can be birth time while fstat().ctime is change time.
    # Compare ctime only between the SAME API before/after, never across them.
    return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,getattr(value,'st_birthtime_ns',None))


class OrdinaryMediaVersions:
    def __init__(self, db, player, *, max_bytes=1024**3, time_limit=2.0, clock=time.monotonic):
        if type(max_bytes) is not int or not 1<=max_bytes<=8*1024**3:
            raise ValueError('invalid media hash byte budget')
        if isinstance(time_limit,bool) or not isinstance(time_limit,(int,float)) or not math.isfinite(time_limit) or not 0<time_limit<=30:
            raise ValueError('invalid media hash time budget')
        self.db,self.player,self.max_bytes,self.time_limit,self.clock=db,player,max_bytes,time_limit,clock
        with db.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS ordinary_media_versions(
                media_key TEXT PRIMARY KEY,path_key TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,
                association_digest TEXT NOT NULL,verified_at REAL NOT NULL)''')

    def binding(self, row):
        path=str(Path(row['path'])).lower()
        key=hashlib.sha256(path.encode()).hexdigest()[:32]
        if row.get('id')!=key:
            raise ValueError('ordinary inventory identity changed')
        sid=row.get('show_id')
        association=(sid,row.get('episode'),self.db.mapping(sid).get('season',1) if sid else None)
        return key,path,hashlib.sha256(repr(association).encode()).hexdigest()

    def content(self, row):
        deadline=self.clock()+self.time_limit
        path=Path(row['path']);digest=hashlib.sha256();count=0
        try:
            with path.open('rb') as stream:
                before=os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=self.max_bytes or before.st_size!=row['size']:
                    raise ValueError('ordinary version unavailable within byte budget')
                while True:
                    if self.clock()>deadline:raise ValueError('ordinary version hash time budget exceeded')
                    data=stream.read(min(1024**2,self.max_bytes-count+1))
                    if not data:break
                    count+=len(data)
                    if count>self.max_bytes:raise ValueError('ordinary version hash byte budget exceeded')
                    digest.update(data)
                after=os.fstat(stream.fileno())
                if count!=before.st_size or fingerprint(before)!=fingerprint(after) or file_identity(after)!=file_identity(path.stat()):
                    raise ValueError('ordinary media changed during verification')
                if self.clock()>deadline:raise ValueError('ordinary version hash time budget exceeded')
        except OSError:
            raise ValueError('ordinary version file unavailable') from None
        return digest.hexdigest()

    def register(self, media_key, expected_sha256):
        """Trusted control-plane only, not a first-request auto-enrolment or progress migration."""
        if not isinstance(media_key,str) or not re.fullmatch('[0-9a-f]{32}',media_key):
            raise ValueError('invalid ordinary identity')
        if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256):
            raise ValueError('trusted content digest required')
        row=self.player.item(media_key);key,path,association=self.binding(row)
        if self.content(row)!=expected_sha256:raise ValueError('trusted digest does not match current media')
        if self.binding(self.player.item(key))!=(key,path,association):
            raise ValueError('ordinary mapping changed during verification')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT path_key,size,sha256,association_digest FROM ordinary_media_versions WHERE media_key=?',(key,)).fetchone()
            wanted=(path,row['size'],expected_sha256,association)
            if old:
                if tuple(old)!=wanted:raise ValueError('ordinary alias rebind forbidden')
                return
            c.execute('INSERT INTO ordinary_media_versions VALUES(?,?,?,?,?,?)',(key,*wanted,time.time()))

    def verify(self, row):
        key,path,association=self.binding(row)
        proof=self.db.one('SELECT * FROM ordinary_media_versions WHERE media_key=?',(key,))
        if not proof:raise ValueError('ordinary media has no trusted version evidence')
        if (proof['path_key'],proof['size'],proof['association_digest'])!=(path,row['size'],association):
            raise ValueError('ordinary media mapping or identity changed')
        if self.content(row)!=proof['sha256']:
            raise ValueError('ordinary content version changed; old alias is fenced')
        if self.binding(self.player.item(key))!=(key,path,association):
            raise ValueError('ordinary mapping changed during verification')
        return proof['sha256']
