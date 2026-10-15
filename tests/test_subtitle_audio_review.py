"""Synthetic audio only, fake local model transport; no external inference."""
import base64
import wave
import pytest
from anime.subtitle_audio_review import LocalAudioReviewer,wav_input


def pcm(tmp_path,seconds=1,rate=16000,channels=1,width=2):
    p=tmp_path/'fixture.wav'
    with wave.open(str(p),'wb') as w:
        w.setnchannels(channels);w.setsampwidth(width);w.setframerate(rate)
        w.writeframes(bytes(int(rate*seconds)*channels*width))
    return p


def fake(calls,capabilities=None,digest='original-fixture-digest',content='Original synthetic audio model observation'):
    def request(route,body=None):
        calls.append((route,body))
        if route=='/api/version':return {'version':'synthetic-fixture'}
        if route=='/api/tags':return {'models':[{'name':'gemma4:e2b','digest':digest}]}
        if route=='/api/show':return {'capabilities':capabilities or ['completion','audio']}
        return {'id':'original-fixture','choices':[{'message':{'content':content},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':100,'completion_tokens':6}}
    return request


def test_audio_bytes_go_to_explicit_audio_input_without_acceptance(tmp_path):
    p=pcm(tmp_path);calls=[]
    record=LocalAudioReviewer(transport=fake(calls)).observe(p,'Describe only the provided audio.',
                                                          expected_model_digest='original-fixture-digest')
    route,body=calls[-1]
    assert route=='/v1/chat/completions'
    part=body['messages'][0]['content'][1]
    assert part['type']=='input_audio' and base64.b64decode(part['input_audio']['data'])==p.read_bytes()
    assert not any('transcript' in k for k in body)
    assert record['status']=='proposal_only' and record['scope']=='local_audio_model_observation_unaccepted'
    assert not record['current_codex_listened'] and not record['human_listened'] and not record['subtitle_acceptance']
    assert 'data' not in record['audio'] and record['audio']['duration_seconds']==1


@pytest.mark.parametrize('endpoint',['https://api.openai.com','http://localhost:11434','http://127.0.0.1:11434','http://example.invalid'])
def test_remote_default_or_unowned_endpoints_are_rejected(endpoint):
    with pytest.raises(ValueError):LocalAudioReviewer(endpoint=endpoint)


@pytest.mark.parametrize('model',['glm-5.2:cloud','gemma4:12b','uninstalled','gemma4'])
def test_cloud_or_unapproved_models_are_rejected(model):
    with pytest.raises(ValueError):LocalAudioReviewer(model=model)


@pytest.mark.parametrize('change',[{'seconds':31},{'seconds':0},{'rate':44100},{'channels':2},{'width':1}])
def test_unbounded_or_unqualified_audio_is_rejected_before_inference(tmp_path,change):
    calls=[]
    with pytest.raises(ValueError):LocalAudioReviewer(transport=fake(calls)).observe(pcm(tmp_path,**change),'Question')
    assert not calls


def test_missing_audio_encoder_changed_model_and_empty_output_do_not_pass(tmp_path):
    p=pcm(tmp_path)
    with pytest.raises(ValueError,match='audio encoder'):
        LocalAudioReviewer(transport=fake([],capabilities=['completion','vision'])).observe(p,'Question')
    calls=[]
    with pytest.raises(ValueError,match='changed'):
        LocalAudioReviewer(transport=fake(calls)).observe(p,'Question',expected_model_digest='other')
    assert all(route!='/v1/chat/completions' for route,_ in calls)
    with pytest.raises(ValueError,match='no review content'):
        LocalAudioReviewer(transport=fake([],content='')).observe(p,'Question')


def test_non_wav_input_is_rejected(tmp_path):
    p=tmp_path/'bad.wav';p.write_bytes(b'not a WAV')
    with pytest.raises(ValueError):wav_input(p)
