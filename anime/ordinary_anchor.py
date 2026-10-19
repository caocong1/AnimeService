"""Isolated signed append-only alias ledger with an INJECTED external monotonic CAS.

There is deliberately no production anchor provider. A file beside this ledger
cannot be its anti-rollback root; missing/unavailable trusted anchors fail closed.
This ledger is not yet wired to OrdinaryIssuer or inventory/registration.
"""
from pathlib import Path
import os
import secrets
import math
import time

from .ordinary_issuance import canonical, digest, hex_value, load, plain, windows_file
from .ordinary_attestation import acl


class AnchoredLedger:
    def __init__(self,root,signer,anchor,*,max_entries=1024,time_limit=30,clock=time.monotonic):
        self.root=Path(root);acl(self.root)
        if (not callable(getattr(anchor,'read',None)) or not callable(getattr(anchor,'compare_exchange',None))
                or not isinstance(getattr(anchor,'identity',None),str) or not hex_value(anchor.identity,32)
                or type(max_entries) is not int or not 1<=max_entries<=10000
                or isinstance(time_limit,bool) or not isinstance(time_limit,(int,float)) or not math.isfinite(time_limit) or not 0<time_limit<=120):
            raise ValueError('external trusted monotonic anchor required')
        self.signer,self.anchor,self.max_entries=signer,anchor,max_entries
        self.clock,self.time_limit=clock,time_limit

    def budget(self,deadline):
        if self.clock()>deadline:raise ValueError('ledger time budget exceeded')

    def head(self):
        value=self.anchor.read()
        if (not isinstance(value,tuple) or len(value)!=2 or type(value[0]) is not int
                or not 0<=value[0]<=self.max_entries or not hex_value(value[1])
                or value[0]==0 and value[1]!='0'*64):
            raise ValueError('trusted anchor unavailable or invalid')
        return value

    def snapshot(self):
        return self._snapshot(self.clock()+self.time_limit)

    def _snapshot(self,deadline):
        self.budget(deadline)
        head=self.head();previous='0'*64;aliases={}
        for seq in range(1,head[0]+1):
            self.budget(deadline)
            path=self.root/(str(seq)+'.json');plain(path);acl(path)
            with windows_file(path) as stream:value,signature=load(stream)
            body=self.signer.unseal(value,'ordinary-ledger/v1')
            if (set(body)!={'schema','anchor_id','sequence','previous','alias','manifest_sha256'}
                    or body['anchor_id']!=self.anchor.identity or type(body['sequence']) is not int or body['sequence']!=seq
                    or body['previous']!=previous or not hex_value(body['alias'],32) or not hex_value(body['manifest_sha256'])
                    or body['alias'] in aliases):
                raise ValueError('ledger downgrade, replay or alias rebind')
            aliases[body['alias']]=body['manifest_sha256'];previous=signature
        if previous!=head[1] or self.head()!=head:raise ValueError('ledger rollback or anchor moved')
        self.budget(deadline)
        return head,aliases

    def bind(self,alias,manifest_sha256):
        if not hex_value(alias,32) or not hex_value(manifest_sha256):raise ValueError('invalid ledger binding')
        deadline=self.clock()+self.time_limit
        with windows_file(self.root/'.anchor.lock',exclusive=True):
            head,aliases=self._snapshot(deadline)
            if alias in aliases:
                if aliases[alias]!=manifest_sha256:raise ValueError('anchored alias rebind forbidden')
                return head
            if head[0]>=self.max_entries:raise ValueError('ledger entry budget exceeded')
            body={'schema':'ordinary-ledger/v1','anchor_id':self.anchor.identity,'sequence':head[0]+1,'previous':head[1],'alias':alias,'manifest_sha256':manifest_sha256}
            path=self.root/(str(head[0]+1)+'.json')
            if path.exists():
                # Crash before CAS: accept only the exact signed pending fact.
                plain(path);acl(path)
                with windows_file(path) as stream:value,signature=load(stream)
                if self.signer.unseal(value,'ordinary-ledger/v1')!=body:raise ValueError('different pending ledger fact')
            else:
                data=canonical(self.signer.seal(body))
                pending=self.root/('.pending-'+secrets.token_hex(16))
                try:
                    with pending.open('xb') as stream:
                        acl(pending,new_file=True);stream.write(data);stream.flush();os.fsync(stream.fileno())
                    os.link(pending,path)  # expose only complete JSON, never overwrite an existing record
                finally:
                    pending.unlink(missing_ok=True)
                signature=digest(data)
            wanted=(head[0]+1,signature)
            self.budget(deadline)
            if self.anchor.compare_exchange(head,wanted) is not True:raise ValueError('anchor CAS rejected or outcome unknown')
            if self.head()!=wanted:raise ValueError('anchor commit not confirmed')
            self.budget(deadline)
            return wanted
