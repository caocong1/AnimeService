import hashlib
import json
import os
from unittest.mock import Mock

import pytest

from anime.picture import Picture, describe
from anime.subtitles import Subtitles
from test_web_ui import client_at


def sample(**values):
    return dict(codec_name='h264', profile='High', width=1920, height=1080,
        pix_fmt='yuv420p', color_primaries='bt709', color_transfer='bt709',
        color_space='bt709', color_range='tv', field_order='progressive',
        sample_aspect_ratio='1:1', avg_frame_rate='24000/1001', **values)


@pytest.mark.parametrize('pix,bits', [('yuv420p',8),('yuv420p10le',10),
    ('gbrp12le',12),('yuv444p16be',16),('unknown',None)])
def test_bit_depth_is_not_guessed_from_codec_or_resolution(pix,bits):
    s=sample();s['pix_fmt']=pix
    d=describe(s)
    assert d['bit_depth']==bits
    assert d['sdr_scaler_eligible']==(bits==8)


@pytest.mark.parametrize('change', [dict(color_transfer='smpte2084'),
    dict(color_transfer='arib-std-b67'),dict(color_transfer='unknown'),
    dict(color_space='unknown'),dict(color_primaries='bt2020'),
    dict(field_order='tt'),dict(sample_aspect_ratio='4:3'),
    dict(side_data_list=[{'side_data_type':'DOVI configuration record'}])])
def test_hdr_unknown_anamorphic_and_interlaced_use_native(change):
    s=sample();s.update(change)
    assert not describe(s)['sdr_scaler_eligible']


def test_probe_cache_invalidation_and_public_api_are_read_only(tmp_path):
    client,db,_=client_at(tmp_path)
    p=tmp_path/'clip.mp4';p.write_bytes(b'fixture')
    key=hashlib.sha256(str(p).lower().encode()).hexdigest()[:32]
    (db.path.parent/'inventory.json').write_text(json.dumps([{'path':str(p),'size':7}]))
    service=client.app.state.webplayer.picture
    service.tools.tool=Mock(return_value='fake-probe')
    service.tools.run=Mock(return_value=json.dumps({'streams':[sample()]}).encode())
    before=db.rows('SELECT * FROM watches'),db.rows('SELECT * FROM web_progress')
    r=client.get(f'/api/web/media/{key}/picture');assert r.status_code==200
    assert r.json()['video_transcoded'] is False and r.json()['sdr_scaler_eligible']
    assert 'path' not in r.json() and 'fixture' not in r.text
    client.get(f'/api/web/media/{key}/picture');assert service.tools.run.call_count==1
    st=p.stat();os.utime(p,ns=(st.st_atime_ns,st.st_mtime_ns+1_000_000))
    client.get(f'/api/web/media/{key}/picture');assert service.tools.run.call_count==2
    assert (db.rows('SELECT * FROM watches'),db.rows('SELECT * FROM web_progress'))==before
    assert client.get('/api/web/media/not-a-media/picture').status_code==404


def test_no_tags_no_sdr_claim_and_bad_fps():
    d=describe({'codec_name':'hevc','width':3840,'height':2160,'avg_frame_rate':'0/0'})
    assert d['dynamic_range']=='unknown' and d['bit_depth'] is None and d['fps'] is None
    assert not d['sdr_scaler_eligible']
