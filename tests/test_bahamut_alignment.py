import json
import sys
import time
from unittest.mock import Mock

import pytest

from anime import audio_alignment as audio
from anime import bahamut_alignment as baha

SOURCE = 'https://ani.gamer.com.tw/animeVideo.php?sn=51083'
BASE = 'https://bahamut.akamaized.net/media/list.m3u8?secret=private'


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(baha, '_limited_until', 0)
    monkeypatch.setattr(baha, 'configured_proxy', lambda:None)
    monkeypatch.setattr(baha, 'validate_public_host', lambda host:None)


@pytest.mark.parametrize('url', [SOURCE, SOURCE.replace('51083','1')])
def test_registered_bahamut_url_boundary(url):
    assert audio.canonical_source(url) == url


@pytest.mark.parametrize('suffix', ['&sn=12','&token=secret','#x','&x=2'])
def test_bahamut_identity_never_accepts_extra_parameters(suffix):
    assert audio.canonical_source(SOURCE+suffix) is None


def playlist(extra=''):
    return '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:20\n'+extra+''.join(
        f'#EXTINF:6,\n{i}.ts\n' for i in range(100))+'#EXT-X-ENDLIST\n'


def test_master_selects_lowest_bandwidth_and_media_preserves_timeline():
    master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720\nhi.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=300000\nlo.m3u8\n'
    assert baha.parse_playlist(master,BASE)['variant'] == 'https://bahamut.akamaized.net/media/lo.m3u8'
    parsed = baha.parse_playlist(playlist('#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x1\n'),BASE)
    assert parsed['duration'] == 600
    assert parsed['segments'][3]['start'] == 18
    assert parsed['segments'][3]['sequence'] == 23
    assert parsed['segments'][3]['key']['iv'] == 1


@pytest.mark.parametrize('tag', ['#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n',
    '#EXT-X-MAP:URI="init"\n','#EXT-X-BYTERANGE:100@0\n',
    '#EXT-X-KEY:METHOD=AES-128,URI="key",KEYFORMAT="com.apple.streamingkeydelivery"\n'])
def test_unsupported_hls_never_becomes_a_matching_guess(tag):
    with pytest.raises(audio.Unavailable): baha.parse_playlist(playlist(tag),BASE)
    with pytest.raises(audio.Unavailable): baha.parse_playlist(playlist().replace('#EXT-X-ENDLIST',''),BASE)


def test_segments_stay_on_public_allowlisted_hosts(monkeypatch):
    for url in ['https://evil.test/a.ts','http://bahamut.akamaized.net/a',
                'https://bahamut.akamaized.net@127.0.0.1/a','https://bahamut.akamaized.net:8443/a']:
        with pytest.raises(audio.Unavailable): baha.validate_url(url)
    monkeypatch.setattr(baha,'validate_public_host',Mock(side_effect=audio.Unavailable()))
    with pytest.raises(audio.Unavailable): baha.validate_url('https://bahamut.akamaized.net/a')


def test_fake_dns_requires_explicit_loopback_proxy_and_never_allows_private_network(monkeypatch):
    def dns(address):
        monkeypatch.setattr(audio.socket,'getaddrinfo',lambda *_args,**_kw:[(0,0,0,'',(address,443))])
    dns('198.18.0.1')
    with pytest.raises(audio.Unavailable): audio.validate_public_host('ani.gamer.com.tw')
    audio.validate_public_host('ani.gamer.com.tw',allow_proxy_dns=True)
    for address in ['127.0.0.1','192.168.1.2','10.0.0.1','169.254.169.254','::1']:
        dns(address)
        with pytest.raises(audio.Unavailable): audio.validate_public_host('ani.gamer.com.tw',allow_proxy_dns=True)


def test_access_1018_stops_and_cools_down_without_followup_requests(monkeypatch):
    source=baha.BahamutReference(SOURCE, lambda:None)
    source.read=Mock(return_value=json.dumps({'error':{'code':1018,'message':'SECRET'}}).encode())
    with pytest.raises(audio.Unavailable) as error: source.prepare()
    assert error.value.reason == 'bahamut_limited'
    assert source.read.call_count == 1
    with pytest.raises(audio.Unavailable): source.prepare()
    assert source.read.call_count == 1
    assert not audio.failure('bahamut_limited')['retryable']
    assert 'SECRET' not in json.dumps(audio.failure('bahamut_limited'))
    source.close()


def test_sample_reads_only_selected_window_and_rejects_discontinuity():
    source=baha.BahamutReference(SOURCE,lambda:None)
    source.segments=baha.parse_playlist(playlist(),BASE)['segments']
    source.read=Mock(return_value=b'ts-data')
    raw,start=source.sample(131.5)
    assert start == 132 and raw == b'ts-data'*6
    assert source.read.call_count == 6
    source.segments[24]['discontinuity']=1
    with pytest.raises(audio.Unavailable):source.sample(132)
    source.close()


def test_normal_aes128_segment_uses_playlist_iv_and_unpads(monkeypatch):
    import yt_dlp.aes as aes
    source=baha.BahamutReference(SOURCE,lambda:None)
    source.segments=baha.parse_playlist(playlist('#EXT-X-KEY:METHOD=AES-128,URI="key"\n'),BASE)['segments']
    secret=bytes(range(16));plain=b'ts-payload'+bytes([6])*6
    def read(url,*args,**kwargs):
        if url.endswith('/key'):return secret
        seq=int(url.rsplit('/',1)[1].split('.')[0])+20
        return bytes(aes.aes_cbc_encrypt(list(plain),list(secret),list(seq.to_bytes(16,'big'))))
    source.read=Mock(side_effect=read)
    raw,start=source.sample(132)
    assert raw == b'ts-payload'*6 and start == 132
    assert len(source.keys)==1
    source.close();assert not source.keys


def test_reference_api_selects_media_and_keeps_device_and_cookies_private():
    source=baha.BahamutReference(SOURCE,lambda:None)
    responses=[{'deviceid':'private-device'}, {'time':1}, {'data':{'srcUseCases':[{'src':{'playlist':BASE}}]}}]
    source.api=Mock(side_effect=responses)
    source.read=Mock(return_value=playlist().encode())
    assert source.prepare() is source and source.duration==600
    assert source.api.call_count==3
    assert all('videoCastcishu' not in call.args[0] for call in source.api.call_args_list)
    source.close()


def test_unfinished_ad_grant_never_fakes_completion_or_requests_media():
    source=baha.BahamutReference(SOURCE,lambda:None)
    source.api=Mock(side_effect=[{'deviceid':'private-device'},{'time':30,'vip':False}])
    source.read=Mock(side_effect=AssertionError('no media without access grant'))
    with pytest.raises(audio.Unavailable) as error:source.prepare()
    assert error.value.reason=='bahamut_ad'
    assert source.api.call_count==2
    source.read.assert_not_called();source.close()


def test_three_bahamut_samples_apply_only_consistent_measured_offset(tmp_path,monkeypatch):
    path=tmp_path/'local';path.write_bytes(b'local')
    service=audio.AlignmentService(tmp_path/'cache',sys.executable,sys.executable,sys.executable)
    monkeypatch.setattr(service,'_work',lambda:None)
    job=service.jobs[service.start({'path':str(path),'size':5},SOURCE,'stable')['job_id']]
    job['deadline']=time.monotonic()+5
    source=Mock(duration=1420)
    source.sample.side_effect=[(b'ts',312),(b'ts',684),(b'ts',1068)]
    monkeypatch.setattr(baha,'BahamutReference',lambda *_:source)
    monkeypatch.setattr(service,'_run',lambda *_args,**_kw:b'\0'*(11025*2*35))
    monkeypatch.setattr(service,'_fingerprint',lambda *_:None)
    hits=[{'reliable':True,'local_time':start+20} for start in (312,684,1068)]
    monkeypatch.setattr(audio,'locate_fingerprint',lambda *_:hits.pop(0))
    result=service._align_bahamut(job,None,1420,tmp_path/'result.json')
    assert result['offset']==20
    source.close.assert_called_once()
    assert 'https' not in (tmp_path/'result.json').read_text()


def test_http_budget_is_shared_across_segments_and_metadata(monkeypatch):
    source=baha.BahamutReference(SOURCE,lambda:None)
    response=Mock(status_code=200)
    response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    response.iter_content.return_value=[b'a'*16]
    source.session.get=Mock(return_value=response)
    monkeypatch.setattr(baha,'LIMIT',20)
    assert source.read(BASE)==b'a'*16
    response.iter_content.return_value=[b'b'*16]
    with pytest.raises(audio.Unavailable) as error:source.read(BASE)
    assert error.value.reason=='budget'
    source.close()
