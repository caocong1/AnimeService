import hashlib
import json
import sys
import threading
import time
from contextlib import nullcontext
from unittest.mock import Mock

import numpy as np
import pytest
import requests

from anime import audio_alignment as a


URL='https://www.bilibili.com/video/BV1EVap6yE6B?p=1'


def fingerprints():
    return np.random.default_rng(8).integers(0,2**32,size=12000,dtype=np.uint32)


@pytest.mark.parametrize('offset',[-20,0,20])
def test_three_spread_anchors_find_real_fingerprint_offset(offset):
    ref=fingerprints();anchors=[]
    for local_time in [300,650,1050]:
        index=round(local_time/a.HOP)
        query=ref[index:index+250].copy()
        # Different encoding: one flipped bit per frame; not an exact byte match.
        query ^= np.uint32(1)
        hit=a.locate_fingerprint(ref,query)
        hit['source_time']=local_time-offset;anchors.append(hit)
    assert a.decide_alignment(anchors,1420)==pytest.approx(offset,abs=.07)


def test_tail_extension_does_not_rescale_and_middle_insertion_is_rejected():
    ref=fingerprints();anchors=[]
    for local in [300,650,1050]:
        hit=a.locate_fingerprint(ref,ref[round(local/a.HOP):round(local/a.HOP)+250])
        hit['source_time']=local;anchors.append(hit)
    assert a.decide_alignment(anchors,1420)==pytest.approx(0,abs=.07)
    anchors[-1]['source_time']+=30
    assert a.decide_alignment(anchors,1420) is None


def test_unrelated_audio_silence_and_repeated_scene_never_match():
    ref=fingerprints()
    other=np.random.default_rng(9).integers(0,2**32,size=250,dtype=np.uint32)
    assert not a.locate_fingerprint(ref,other)['reliable']
    assert not a.locate_fingerprint(ref,np.zeros(250,dtype=np.uint32))['reliable']
    query=ref[2000:2250].copy();ref[7000:7250]=query
    assert not a.locate_fingerprint(ref,query)['reliable']


@pytest.mark.parametrize('url',[
    'http://www.bilibili.com/video/BV1EVap6yE6B', 'https://www.bilibili.com@127.0.0.1/video/BV1EVap6yE6B',
    URL+'&token=secret',URL+'&p=2',URL+'#foo','https://www.bilibili.com/video/BV1EVap6yE6B?p=0',
    'https://ani.gamer.com.tw/animeVideo.php?sn=1'])
def test_source_boundary(url):assert a.canonical_source(url) is None


def test_cdn_rejects_unknown_and_private_dns(monkeypatch):
    monkeypatch.setattr(a.socket,'getaddrinfo',lambda *_args,**_kw:[(0,0,0,'',('127.0.0.1',443))])
    for url in ['https://evil.test/a','http://upos.bilivideo.com/a','https://upos.bilivideo.com/a']:
        with pytest.raises(a.Unavailable):a.validate_cdn(url)


def service(tmp_path,monkeypatch):
    path=tmp_path/'episode';path.write_bytes(b'local')
    s=a.AlignmentService(tmp_path/'cache',sys.executable,sys.executable,sys.executable)
    return s,{'id':'media','path':str(path),'size':5}


def finished(s,job):
    deadline=time.monotonic()+3
    while s.status(job)['status'] in a.ACTIVE and time.monotonic()<deadline:time.sleep(.01)
    assert s.status(job)['status'] not in a.ACTIVE
    return s.status(job)


def test_start_coalesces_and_cancelled_inflight_result_cannot_apply(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch);entered=threading.Event();gate=threading.Event()
    def align(job):entered.set();gate.wait(2);return {'status':'matched','offset':20}
    monkeypatch.setattr(s,'_align',align)
    first=s.start(item,URL,'stable');assert entered.wait(1)
    assert s.start(item,URL,'stable',force=True)['job_id']==first['job_id']
    s.cancel(first['job_id']);gate.set();s.queue.join()
    result=finished(s,first['job_id']);assert result['status']=='cancelled';assert 'offset' not in result
    assert item['path'] not in json.dumps(result)


def test_modified_media_and_exception_text_are_redacted(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch)
    def align(job):
        from pathlib import Path
        Path(item['path']).write_bytes(b'other size')
        raise RuntimeError('https://upos.bilivideo.com/a?token=SECRET')
    monkeypatch.setattr(s,'_align',align)
    result=s.start(item,URL,'stable');public=finished(s,result['job_id'])
    assert public['status']=='unavailable';assert 'SECRET' not in json.dumps(public)
    assert item['path'] not in json.dumps(public)
    assert s.start(item,URL,'stable')['status']=='unavailable'


def test_single_worker_and_bounded_queue(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch);entered=threading.Event();gate=threading.Event();calls=[]
    def align(job):calls.append(job['job_id']);entered.set();gate.wait(3);return {'status':'unreliable'}
    monkeypatch.setattr(s,'_align',align)
    s.start(item,URL,'stable');assert entered.wait(1)
    results=[s.start(item,URL.replace('p=1',f'p={p}'),str(p)) for p in range(2,27)]
    assert len(calls)==1;assert sum(r['status']=='queued' for r in results)==20
    assert sum(r['status']=='unavailable' for r in results)==5
    for job in list(s.jobs):s.cancel(job)
    gate.set();s.queue.join()


def test_cached_success_survives_restart_without_source_network(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch)
    monkeypatch.setattr(s,'_work',lambda:None)
    job=s.jobs[s.start(item,URL,'stable')['job_id']];job['deadline']=time.monotonic()+20
    monkeypatch.setattr(s,'_probe',lambda _: (1420,0))
    key=hashlib.sha256(json.dumps([a.ALGORITHM,item['path'],5,job['mtime'],0]).encode()).hexdigest()
    result_key=hashlib.sha256(f'{key}|{job["pair"]}'.encode()).hexdigest()
    anchors=[{'reliable':True,'local_time':x,'source_time':x-20} for x in (300,650,1050)]
    s._cache_write(s.cache_dir/(result_key+'.json'),{'status':'matched','offset':20,'anchors':anchors,'algorithm':a.ALGORITHM,'remote_id':'BV-cid'})
    monkeypatch.setattr(s,'_source_audio',Mock(side_effect=AssertionError('must not contact source')))
    assert s._align(job)['offset']==20;s._source_audio.assert_not_called()
    from pathlib import Path
    Path(item['path']).touch()
    with pytest.raises(a.Unavailable):s._check(job)


class Response:
    def __init__(self,status,start=0,end=65535):
        import io
        self.status_code=status;self.headers={'Content-Length':str(end-start+1),'Content-Range':f'bytes {start}-{end}/200000','Content-Type':'audio/mp4'}
        self.raw=io.BytesIO(b'a'*(end-start+1))
    def iter_content(self,chunk_size):
        for _ in range(8):yield b'a'*8192
    def close(self):pass


def test_range_proxy_hard_budget_and_no_seek_fallback(monkeypatch):
    monkeypatch.setattr(a,'MAX_BYTES',65536)
    monkeypatch.setattr(a,'validate_cdn',lambda _:None)
    for status in [206,200]:
        proxy=a.RangeProxy('https://upos.bilivideo.com/a?token=SECRET',{'Referer':'https://www.bilibili.com/'},lambda:None)
        proxy.session.request=Mock(return_value=Response(status))
        with proxy as url:
            first=requests.get(url,headers={'Range':'bytes=0-'},timeout=3)
            if status==206:
                assert first.status_code==206;assert len(first.content)==65536
                assert requests.get(url,headers={'Range':'bytes=0-65535'},timeout=3).content==first.content
                assert requests.get(url,headers={'Range':'bytes=65536-'},timeout=3).status_code==502
                assert proxy.downloaded==65536;assert proxy.session.request.call_count==1
                assert proxy.error_reason=='budget'
            else:
                assert first.status_code==502;assert proxy.downloaded==0;assert proxy.failed
            assert 'SECRET' not in first.text


def test_local_reference_protocols_pcm_cap_and_cancel(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch);monkeypatch.setattr(s,'_work',lambda:None)
    job=s.jobs[s.start(item,URL,'stable')['job_id']];job['deadline']=time.monotonic()+3
    with pytest.raises(a.Unavailable):s._run(job,[sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*100000)'],cap=20000)
    s.cancel(job['job_id'])
    with pytest.raises(a.Cancelled):s._run(job,[sys.executable,'-c','print(1)'])


def test_parallel_seek_does_not_wait_for_abandoned_probe_connection(monkeypatch):
    monkeypatch.setattr(a,'validate_cdn',lambda _:None)
    total=2*1024*1024
    data=bytes(range(251))*(total//251+1)
    def response(_method,_url,headers,**_kw):
        import io
        start,end=headers['Range'][6:].split('-');start=int(start);end=int(end) if end else total-1
        r=Response(206,start,end);r.headers['Content-Range']=f'bytes {start}-{end}/{total}'
        r.raw=io.BytesIO(data[start:end+1]);return r
    proxy=a.RangeProxy('https://upos.bilivideo.com/a',{},lambda:None)
    proxy.session.request=Mock(side_effect=response)
    with proxy as url:
        probe=requests.get(url,stream=True,timeout=3)
        try:
            began=time.monotonic()
            seek=requests.get(url,headers={'Range':'bytes=1500000-1500511'},timeout=3)
            assert seek.content==data[1500000:1500512]
            assert time.monotonic()-began<2
            assert proxy.downloaded<a.MAX_BYTES
        finally:probe.close()


@pytest.mark.parametrize('reason,retryable', [('source_info',True),('source_audio',True),('timeout',True),
    ('busy',True),('tool',False),('budget',False),('no_source_audio',False),('media_changed',False)])
def test_failure_reasons_are_safe_and_only_transient_failures_retry(tmp_path,monkeypatch,reason,retryable):
    s,item=service(tmp_path,monkeypatch)
    def align(job):raise a.Unavailable(reason)
    monkeypatch.setattr(s,'_align',align)
    result=s.start(item,URL,'stable');public=finished(s,result['job_id'])
    assert public['reason']==reason and public['retryable'] is retryable
    assert item['path'] not in json.dumps(public)
    assert not a.failure('https://invalid.test/?token=SECRET')['retryable']
    assert 'SECRET' not in json.dumps(a.failure('https://invalid.test/?token=SECRET'))


def test_transient_failed_job_force_retries_instead_of_returning_failure_cache(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch);calls=[]
    def align(job):
        calls.append(job['job_id'])
        if len(calls)==1:raise a.Unavailable('source_info')
        return {'status':'matched','offset':.06}
    monkeypatch.setattr(s,'_align',align)
    first=s.start(item,URL,'stable');assert finished(s,first['job_id'])['retryable']
    assert s.start(item,URL,'stable')['job_id']==first['job_id']
    second=s.start(item,URL,'stable',force=True)
    assert second['job_id']!=first['job_id']
    assert finished(s,second['job_id'])['status']=='matched'


def test_missing_dependency_tool_and_changed_media_are_distinct(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch)
    s.fpcalc=str(tmp_path/'missing.exe')
    assert s.start(item,URL,'stable')['reason']=='tool'
    s.fpcalc=sys.executable
    assert s.start({**item,'size':1},URL,'stable')['reason']=='media_changed'
    monkeypatch.setitem(sys.modules,'yt_dlp',None)
    assert s.start(item,URL,'stable')['reason']=='dependency'


@pytest.mark.parametrize('formats,duration,reason', [([],500,'no_source_audio'),
    ([{'vcodec':'none','acodec':'aac','url':'https://upos.bilivideo.com/audio'}],11,'short_source')])
def test_unreadable_or_short_source_never_retries(tmp_path,monkeypatch,formats,duration,reason):
    s,item=service(tmp_path,monkeypatch);monkeypatch.setattr(s,'_work',lambda:None)
    job=s.jobs[s.start(item,URL,'stable')['job_id']];job['deadline']=time.monotonic()+2
    fake=Mock();fake.extract_info.return_value={'formats':formats,'duration':duration}
    import yt_dlp
    monkeypatch.setattr(yt_dlp,'YoutubeDL',lambda _:nullcontext(fake))
    monkeypatch.setattr(a,'validate_cdn',lambda _:None)
    with pytest.raises(a.Unavailable) as error:s._source_audio(job)
    assert error.value.reason==reason and not a.failure(reason)['retryable']


def test_local_probe_missing_audio_reports_local_error(tmp_path,monkeypatch):
    s,item=service(tmp_path,monkeypatch)
    monkeypatch.setattr(s,'_run',lambda *_args,**_kwargs:json.dumps({'format':{'duration':'1420'},'streams':[]}).encode())
    with pytest.raises(a.Unavailable) as error:s._probe({'item':item})
    assert error.value.reason=='local_audio'
