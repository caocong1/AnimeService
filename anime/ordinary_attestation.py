"""Opt-in custody evidence and timed keys. No production caller, DB, Engine or network.

Intake approval is an independent trusted callback, never an HTTP/file-path request.
HMAC verifiers are trusted signers too; this is not a public-key trust boundary.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from types import SimpleNamespace

from .ordinary_issuance import OrdinaryIssuer, canonical, digest, hex_value, load, plain, windows_file
from .playback_versions import fingerprint, file_identity


@dataclass(frozen=True)
class TimedKey:
    key_id: str
    purpose: str
    not_before: int
    not_after: int
    secret: bytes = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.key_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',self.key_id)
                or self.purpose not in ('source','publication')
                or type(self.not_before) is not int or type(self.not_after) is not int
                or not 0 <= self.not_before < self.not_after
                or not isinstance(self.secret,bytes) or len(self.secret)!=32):
            raise ValueError('invalid timed key')

    @classmethod
    def generate(cls,purpose,not_before,not_after):
        return cls(secrets.token_hex(16),purpose,not_before,not_after,secrets.token_bytes(32))


def local_plain(path):
    path=Path(path)
    if os.name!='nt' or not path.is_absolute() or not re.fullmatch('[A-Za-z]:',path.drive):
        raise ValueError('local Windows fixed drive required')
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetDriveTypeW.argtypes=[wintypes.LPCWSTR];kernel.GetDriveTypeW.restype=wintypes.UINT
    if kernel.GetDriveTypeW(path.anchor)!=3:raise ValueError('local Windows fixed drive required')
    return plain(path)


def acl(path, *, create_directory=False, new_file=False):
    """Set only a NEW owned directory; otherwise validate directory/file ACL read-only."""
    path=local_plain(path)
    if os.name!='nt':
        raise ValueError('Windows private storage required')
    encoded=base64.b64encode(str(path).encode()).decode()
    script=r'''$ErrorActionPreference='Stop'
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PATH_B64'))
$u=[Security.Principal.WindowsIdentity]::GetCurrent().User
$s=[Security.Principal.SecurityIdentifier]::new('S-1-5-18')
if(SET_NEW) {
 $a=[Security.AccessControl.DirectorySecurity]::new();$a.SetOwner($u);$a.SetAccessRuleProtection($true,$false)
 foreach($sid in @($u,$s)){$a.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))}
 [IO.Directory]::SetAccessControl($p,$a)
}
if(SET_FILE_OWNER){$fresh=[IO.File]::GetAccessControl($p);$fresh.SetOwner($u);[IO.File]::SetAccessControl($p,$fresh)}
if([IO.Directory]::Exists($p)){$a=[IO.Directory]::GetAccessControl($p);if(-not $a.AreAccessRulesProtected){throw 'unprotected'}}
else{$a=[IO.File]::GetAccessControl($p)}
$r=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]));$expected=@($u.Value)
if($s.Value -notin $expected){$expected+=$s.Value}
if($a.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $expected){throw 'unexpected owner'}
if($r.Count -ne $expected.Count){throw 'unexpected rules'}
foreach($x in $r){if($x.IdentityReference.Value -notin $expected -or $x.AccessControlType -ne 'Allow' -or $x.FileSystemRights -ne 'FullControl'){throw 'unexpected rule'}}
'''.replace('PATH_B64',encoded).replace('SET_NEW','$true' if create_directory else '$false').replace('SET_FILE_OWNER','$true' if new_file else '$false')
    shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        r=subprocess.run([str(shell),'-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16le')).decode()],capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode:raise ValueError('private storage ACL rejected')
    except (OSError,subprocess.SubprocessError):
        raise ValueError('private storage ACL unavailable') from None


def create_private_directory(path):
    path=Path(path);local_plain(path.parent)
    path.mkdir()  # exclusive: never tighten/change an existing directory or parent
    acl(path,create_directory=True)
    return path


class Keyring:
    def __init__(self,keys):
        keys=tuple(keys)
        if (not keys or len(keys)>32 or any(type(k) is not TimedKey for k in keys)
                or len({k.key_id for k in keys})!=len(keys)
                or len({k.secret for k in keys})!=len(keys)):
            raise ValueError('invalid or reused key material')
        self._keys={k.key_id:k for k in keys}

    def key(self,key_id,purpose,now,issued_at=None,expires_at=None):
        key=self._keys.get(key_id) if isinstance(key_id,str) else None
        if (key is None or key.purpose!=purpose or isinstance(now,bool)
                or not isinstance(now,(int,float)) or not math.isfinite(now)
                or not key.not_before<=now<key.not_after
                or issued_at is not None and (type(issued_at) is not int or not key.not_before<=issued_at<key.not_after)
                or expires_at is not None and (type(expires_at) is not int or expires_at>key.not_after)):
            raise ValueError('key unavailable or outside validity interval')
        return key.secret

    def save_new(self,path):
        path=Path(path);acl(path.parent)
        if not re.fullmatch('[A-Za-z0-9_.-]{1,80}',path.name) or path.name in ('.','..'):
            raise ValueError('invalid private key filename')
        body={'schema':'ordinary-keyring/v1','keys':[{'key_id':k.key_id,'purpose':k.purpose,'not_before':k.not_before,'not_after':k.not_after,'secret':base64.b64encode(k.secret).decode()} for k in self._keys.values()]}
        pending=path.parent/('.pending-'+secrets.token_hex(16))
        try:
            with pending.open('xb') as stream:
                acl(pending,new_file=True);stream.write(canonical(body));stream.flush();os.fsync(stream.fileno())
            os.link(pending,path)
        finally:pending.unlink(missing_ok=True)

    @classmethod
    def read_private(cls,path):
        path=local_plain(path);acl(path.parent);acl(path)
        with windows_file(path) as stream:value,_=load(stream)
        if not isinstance(value,dict) or set(value)!={'schema','keys'} or value['schema']!='ordinary-keyring/v1' or not isinstance(value['keys'],list):
            raise ValueError('invalid private keyring')
        try:
            records=[]
            for k in value['keys']:
                if set(k)!={'key_id','purpose','not_before','not_after','secret'}:raise ValueError()
                records.append(TimedKey(k['key_id'],k['purpose'],k['not_before'],k['not_after'],base64.b64decode(k['secret'],validate=True)))
            return cls(records)
        except (TypeError,ValueError,KeyError):
            raise ValueError('invalid private keyring') from None


class SourceAttestor:
    def __init__(self,keys,key_id,approve_intake,*,publisher='owner-intake',max_bytes=8*1024**3,time_limit=120,max_workers=1,lifetime=3600,clock=time.monotonic,wall_clock=time.time):
        if (type(keys) is not Keyring or not callable(approve_intake) or type(max_bytes) is not int or not 1<=max_bytes<=64*1024**3
                or isinstance(time_limit,bool) or not isinstance(time_limit,(int,float)) or not math.isfinite(time_limit) or not 0<time_limit<=600
                or type(max_workers) is not int or not 1<=max_workers<=3 or type(lifetime) is not int or not 1<=lifetime<=86400
                or not isinstance(publisher,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,64}',publisher)):
            raise ValueError('invalid trusted attestation configuration')
        self.keys,self.key_id,self.approve,self.publisher=keys,key_id,approve_intake,publisher
        self.max_bytes,self.time_limit,self.lifetime=max_bytes,time_limit,lifetime
        self.clock,self.wall_clock=clock,wall_clock;self._slots=threading.BoundedSemaphore(max_workers)

    def approved(self,intake):
        if not hex_value(intake,32):raise ValueError('explicit intake identifier required')
        row=self.approve(intake)
        if (not isinstance(row,dict) or row.get('intake_id')!=intake or row.get('approved') is not True
                or row.get('mode') not in ('custody-handoff/v1','owner-reviewed-import/v1')):
            raise ValueError('independent intake approval required')
        return OrdinaryIssuer.binding(row)|{'intake_id':intake,'mode':row['mode']}

    def attest(self,intake):
        if not self._slots.acquire(blocking=False):raise ValueError('attestation concurrency budget exceeded')
        try:
            deadline=self.clock()+self.time_limit;row=self.approved(intake)
            if row['size']>self.max_bytes:raise ValueError('attestation byte budget exceeded')
            now=int(self.wall_clock());expires=now+self.lifetime
            secret=self.keys.key(self.key_id,'source',now,now,expires)
            with windows_file(local_plain(row['path'])) as stream:
                result,count=hashlib.sha256(),0;before=os.fstat(stream.fileno())
                if before.st_size!=row['size']:raise ValueError('intake size moved')
                while True:
                    if self.clock()>deadline:raise ValueError('attestation time budget exceeded')
                    data=stream.read(min(1024**2,self.max_bytes-count+1))
                    if not data:break
                    count+=len(data)
                    if count>self.max_bytes:raise ValueError('attestation byte budget exceeded')
                    result.update(data)
                if (count!=row['size'] or fingerprint(before)!=fingerprint(os.fstat(stream.fileno()))
                        or file_identity(before)!=file_identity(Path(row['path']).stat()) or self.approved(intake)!=row):
                    raise ValueError('intake content or authorization moved')
                if self.clock()>deadline:raise ValueError('attestation time budget exceeded')
                self.keys.key(self.key_id,'source',self.wall_clock(),now,expires)
                body={'schema':'ordinary-source/v1','key_id':self.key_id,'payload':{'issuance_id':secrets.token_hex(16),'issued_at':now,'expires_at':expires,'source_key':row['id'],'sha256':result.hexdigest(),'size':count,'association_digest':row['association_digest'],'input_signature':row['input_signature'],'publisher':self.publisher}}
                body['mac']=hmac.digest(secret,b'ordinary-source/v1\0'+canonical(body),'sha256').hex()
                return body
        except OSError:
            raise ValueError('intake file unavailable') from None
        finally:self._slots.release()


class TimedOrdinaryIssuer(OrdinaryIssuer):
    def __init__(self,root,keys,publication_key_id,resolve_source,**kwargs):
        if type(keys) is not Keyring:raise ValueError('trusted keyring required')
        wall=kwargs.get('wall_clock',time.time);secret=keys.key(publication_key_id,'publication',wall())
        # Keep genuine role-specific material even in the inherited snapshot;
        # no placeholder signing key may become an accidental verification path.
        source_keys={k.key_id:k.secret for k in keys._keys.values() if k.purpose=='source'}
        super().__init__(root,source_keys,secret,resolve_source,publication_key_id=publication_key_id,**kwargs)
        self.keyring=keys

    def source(self,envelope,at=None):
        if not isinstance(envelope,dict) or not isinstance(envelope.get('payload'),dict):raise ValueError('source attestation required')
        p=envelope['payload'];key=self.keyring.key(envelope.get('key_id'),'source',self.wall_clock(),p.get('issued_at'),p.get('expires_at'))
        return OrdinaryIssuer.source(SimpleNamespace(keys={envelope['key_id']:key},wall_clock=self.wall_clock),envelope,at)

    def seal(self,body):
        secret=self.keyring.key(self.key_id,'publication',self.wall_clock())
        return {'body':body,'key_id':self.key_id,'mac':hmac.digest(secret,body['schema'].encode()+b'\0'+canonical(body),'sha256').hex()}

    def unseal(self,envelope,schema):
        if (not isinstance(envelope,dict) or set(envelope)!={'body','key_id','mac'} or not isinstance(envelope['body'],dict)
                or envelope['body'].get('schema')!=schema or not hex_value(envelope['mac'])):raise ValueError('untrusted publication manifest')
        key=self.keyring.key(envelope['key_id'],'publication',self.wall_clock())
        wanted=hmac.digest(key,schema.encode()+b'\0'+canonical(envelope['body']),'sha256').hex()
        if not hmac.compare_digest(envelope['mac'],wanted):raise ValueError('publication signature mismatch')
        return envelope['body']
