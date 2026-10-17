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


def test_failed_copy_leaves_no_partial_and_reports_the_remux(tmp_path):
    p=tmp_path/'source.mp4';p.write_bytes(atom(b'ftyp')+atom(b'moov')+atom(b'mdat')*4)
    sub=Mock();sub.db.path=tmp_path/'test.db';sub.db.get.return_value=8*1024**3;sub.tool.return_value='fakeffmpeg';sub.fingerprint.return_value='c'*64
    def run(args,timeout):
        Path(args[-1]).write_bytes(b'half');raise ValueError('未找到字幕读取工具，请用弹弹play打开')
    sub.run.side_effect=run;cache=BrowserMedia(sub)
    with pytest.raises(ValueError,match='转封装失败'):cache.file({'path':str(p),'size':p.stat().st_size})
    assert not list(cache.root.glob('*.partial.mp4')) and not list(cache.root.glob('*.mp4'))


def test_cache_evicts_oldest_copies_beyond_the_limit_but_keeps_the_new_one(tmp_path):
    p=tmp_path/'source.mp4';raw=atom(b'ftyp')+atom(b'moov')+atom(b'mdat')*4;p.write_bytes(raw)
    sub=Mock();sub.db.path=tmp_path/'test.db';sub.db.get.return_value=25;sub.tool.return_value='fakeffmpeg'
    sub.run.side_effect=lambda args,timeout:Path(args[-1]).write_bytes(b'0123456789')
    cache=BrowserMedia(sub);item={'path':str(p),'size':len(raw)}
    outs=[]
    for i,fp in enumerate(('a'*64,'b'*64,'c'*64)):
        sub.fingerprint.return_value=fp;out=cache.file(item);outs.append(out)
        import os,time
        os.utime(out,(1000+i,1000+i))
    assert outs[2].exists() and outs[1].exists() and not outs[0].exists()  # 10+10 fits in 25, the third evicts the oldest
    assert not (cache.root/('a'*64+'.json')).exists() and (cache.root/('c'*64+'.json')).exists()
