"""Local audio-model observations; never subtitle acceptance or human listening.

Only an isolated loopback Ollama instance and already installed Gemma audio
models are permitted. No downloader, credentials, database or playback writes.
"""
import base64
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path


MODELS={'gemma4:e2b','gemma4:e4b'}
ENDPOINT='http://127.0.0.1:11436'


def wav_input(path):
    path=Path(path)
    if path.stat().st_size>4*1024**2:raise ValueError('Audio review input exceeds the bounded local clip limit')
    raw=path.read_bytes()
    if len(raw)>4*1024**2:raise ValueError('Audio review input changed beyond the bounded local clip limit')
    try:
        with wave.open(io.BytesIO(raw),'rb') as w:
            info={'sample_rate':w.getframerate(),'channels':w.getnchannels(),
                  'sample_width':w.getsampwidth(),'frames':w.getnframes()}
            if w.getcomptype()!='NONE':raise ValueError('Uncompressed PCM WAV required')
    except (wave.Error,EOFError):raise ValueError('Valid PCM WAV required') from None
    duration=info['frames']/info['sample_rate'] if info['sample_rate'] else 0
    if info['sample_rate']!=16000 or info['channels']!=1 or info['sample_width']!=2:
        raise ValueError('Use an explicitly selected 16 kHz mono PCM16 clip')
    if not 0<duration<=30:raise ValueError('Each local audio-model input must be at most 30 seconds')
    return raw,{**info,'duration_seconds':duration,'sha256':hashlib.sha256(raw).hexdigest()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError('Local audio review must not redirect to another service')


class LocalAudioReviewer:
    def __init__(self,model='gemma4:e2b',endpoint=ENDPOINT,transport=None):
        if endpoint!=ENDPOINT:raise ValueError('Only the isolated local audio-review endpoint is allowed')
        if model not in MODELS:raise ValueError('Only already installed local Gemma audio models are allowed')
        self.model=model;self.endpoint=endpoint;self.transport=transport or self._request

    def _request(self,route,body=None):
        if route not in ('/api/version','/api/tags','/api/show','/v1/chat/completions'):
            raise ValueError('Unsupported local audio review operation')
        data=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None
        request=urllib.request.Request(self.endpoint+route,data=data,headers={'Content-Type':'application/json'})
        # Ignore environment proxies and prevent redirects. No credential is needed.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
        with opener.open(request,timeout=60) as response:
            raw=response.read(2*1024**2+1)
        if len(raw)>2*1024**2:raise ValueError('Local model response exceeds the bounded review limit')
        return json.loads(raw)

    def capability(self):
        version=self.transport('/api/version').get('version')
        models=self.transport('/api/tags').get('models',[])
        installed=next((m for m in models if m.get('name')==self.model),None)
        if not installed or not installed.get('digest'):raise ValueError('Requested model is not already installed')
        details=self.transport('/api/show',{'model':self.model})
        if 'audio' not in details.get('capabilities',[]):raise ValueError('Installed runtime does not expose an audio encoder')
        return {'endpoint':self.endpoint,'runtime':'ollama','version':version,'model':self.model,
                'model_digest':installed['digest'],'capabilities':details['capabilities']}

    def observe(self,path,prompt,expected_model_digest=None,max_tokens=640):
        if not isinstance(prompt,str) or not prompt.strip() or len(prompt)>12000:
            raise ValueError('A bounded, explicit audio review question is required')
        if type(max_tokens) is not int or not 1<=max_tokens<=1024:raise ValueError('Review output token limit invalid')
        raw,audio=wav_input(path);capability=self.capability()
        if expected_model_digest and capability['model_digest']!=expected_model_digest:
            raise ValueError('Installed audio model changed since the capability control')
        body={'model':self.model,'messages':[{'role':'user','content':[
            {'type':'text','text':prompt},
            {'type':'input_audio','input_audio':{'data':base64.b64encode(raw).decode('ascii'),'format':'wav'}}]}],
            'temperature':0,'reasoning_effort':'none','max_tokens':max_tokens,'stream':False}
        started=time.time();clock=time.perf_counter();response=self.transport('/v1/chat/completions',body)
        choices=response.get('choices')
        if not isinstance(choices,list) or not choices or not isinstance(choices[0],dict):
            raise ValueError('Local audio model returned no review content')
        content=choices[0].get('message',{}).get('content')
        if not isinstance(content,str) or not content.strip():raise ValueError('Local audio model returned no review content')
        return {'schema_version':1,'status':'proposal_only','scope':'local_audio_model_observation_unaccepted',
                'current_codex_listened':False,'human_listened':False,'subtitle_acceptance':False,
                'capability':capability,'audio':audio,'question':prompt,'observation':content,
                'usage':response.get('usage',{}),'started_at_unix':started,'completed_at_unix':time.time(),
                'elapsed_seconds':round(time.perf_counter()-clock,3),'input_protocol':'input_audio/wav',
                'response_id':response.get('id'),'finish_reason':choices[0].get('finish_reason')}
