import json,struct
from pathlib import Path
from unittest.mock import Mock
import pytest
from anime.browser_media import BrowserMedia,needs_normalization


def atom(kind,data=b''):
    return struct.pack('>I4s',8+len(data),kind)+data


def test_normalization_only_for_valid_repeated_mdat_layout(tmp_path):
    p=tmp_path/'source.mp4'
    for data,expected in [(b'not a movie',False),(atom(b'ftyp')+atom(b'moov')+atom(b'mdat'),False),
                          (atom(b'ftyp')+atom(b'moov')+atom(b'mdat')*4,True),
                          (atom(b'ftyp')+atom(b'moof')+atom(b'mdat')*4,False)]:
        p.write_bytes(data);assert needs_normalization(p)==expected


def test_copy_cache_keeps_original_and_invalidates_on_file_change(tmp_path):
    p=tmp_path/'source.mp4';raw=atom(b'ftyp')+atom(b'moov')+atom(b'mdat')*4;p.write_bytes(raw)
    sub=Mock();sub.db.path=tmp_path/'test.db';sub.tool.return_value='fakeffmpeg'
    sub.fingerprint.return_value='a'*64
    def run(args,timeout):
        assert args[args.index('-c')+1]=='copy' and '-movflags' in args
        Path(args[-1]).write_bytes(b'normalized')
    sub.run.side_effect=run;cache=BrowserMedia(sub);item={'path':str(p),'size':len(raw)}
    out=cache.file(item);assert out.read_bytes()==b'normalized' and p.read_bytes()==raw
    assert cache.file(item)==out and sub.run.call_count==1
    sub.fingerprint.return_value='b'*64;out2=cache.file(item)
    assert out2!=out and sub.run.call_count==2 and out.exists() and p.read_bytes()==raw
