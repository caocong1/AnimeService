"""The supervisor's decisions without Windows: probing, hang detection, clean stop, backoff and log rotation."""
import importlib.util,io,json,urllib.error
from pathlib import Path
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location('supervisor',Path(__file__).resolve().parents[1]/'scripts/supervisor.py')
supervisor=importlib.util.module_from_spec(spec);spec.loader.exec_module(supervisor)


class Clock:
    def __init__(self):self.now=1000.0
    def time(self):return self.now
    def sleep(self,seconds):self.now+=seconds


class Child:
    def __init__(self,exit_at=None):self.exit_at=exit_at;self.returncode=None;self.pid=4242;self.clock=None
    def poll(self):
        if self.exit_at is not None and self.clock.now>=self.exit_at:self.returncode=0
        return self.returncode


def opener_for(answers):
    def opener(url,timeout):
        answer=answers[url.rsplit('/',1)[1]]
        if isinstance(answer,Exception):raise answer
        return io.BytesIO(json.dumps(answer).encode())
    return opener


def test_probe_distinguishes_ours_other_and_down():
    assert supervisor.probe('http://x',opener_for({'health':{'service':'AnimeService','ok':True}}))=='ours'
    assert supervisor.probe('http://x',opener_for({'health':{'service':'Else'}}))=='other'
    old=urllib.error.HTTPError('u',404,'nf',{},None)  # a service without /api/health still answers /api/status
    assert supervisor.probe('http://x',opener_for({'health':old,'status':{'service':'AnimeService'}}))=='ours'
    assert supervisor.probe('http://x',opener_for({'health':old,'status':{'hello':1}}))=='other'
    assert supervisor.probe('http://x',opener_for({'health':urllib.error.HTTPError('u',500,'x',{},None)}))=='other'
    assert supervisor.probe('http://x',opener_for({'health':urllib.error.URLError('refused')}))=='down'
    assert supervisor.probe('http://x',opener_for({'health':TimeoutError()}))=='down'


def test_authority_follows_the_deployment_bind_host(tmp_path):
    assert supervisor.authority(tmp_path)=='http://127.0.0.1:4871'
    (tmp_path/'data').mkdir();(tmp_path/'data/deployment.json').write_text(json.dumps({'bind_host':'0.0.0.0'}))
    assert supervisor.authority(tmp_path)=='http://127.0.0.1:4871'
    (tmp_path/'data/deployment.json').write_text(json.dumps({'bind_host':'192.168.1.5'}))
    assert supervisor.authority(tmp_path)=='http://192.168.1.5:4871'
    (tmp_path/'data/deployment.json').write_text(json.dumps({'bind_host':'::1'}))
    assert supervisor.authority(tmp_path)=='http://[::1]:4871'


def test_hung_child_is_terminated_after_consecutive_failed_probes(tmp_path):
    clock=Clock();child=Child();child.clock=clock;stop=tmp_path/'stop.request';terminate=Mock()
    probes=iter(['ours','ours','down','down','down','ours','down','down','down','down','down','down'])
    outcome=supervisor.watch(child,'http://x',stop=stop,prober=lambda base:next(probes),sleep=clock.sleep,now=clock.time,terminate=terminate)
    assert outcome=='hung' and terminate.call_count==1 and clock.now-1000>=12*30


def test_exit_and_clean_stop_are_told_apart(tmp_path):
    clock=Clock();child=Child(exit_at=1100);child.clock=clock;stop=tmp_path/'stop.request';terminate=Mock()
    assert supervisor.watch(child,'http://x',stop=stop,prober=lambda base:'ours',sleep=clock.sleep,now=clock.time,terminate=terminate)=='exited'
    assert terminate.call_count==0
    clock=Clock();child=Child(exit_at=1020);child.clock=clock;stop.write_text('')
    assert supervisor.watch(child,'http://x',stop=stop,prober=lambda base:'ours',sleep=clock.sleep,now=clock.time,terminate=terminate)=='stopped'
    assert terminate.call_count==0  # it left within the grace period on its own
    clock=Clock();child=Child();child.clock=clock
    assert supervisor.watch(child,'http://x',stop=stop,prober=lambda base:'ours',sleep=clock.sleep,now=clock.time,terminate=terminate,grace=5)=='stopped'
    assert terminate.call_count==1 and clock.now-1000<=7


def test_backoff_and_log_rotation(tmp_path):
    assert [supervisor.backoff(n) for n in (0,1,2,3,7,8)]==[5,5,10,20,300,300]
    path=tmp_path/'supervisor.log';path.write_bytes(b'x'*11)
    supervisor.rotate(path,limit=10)
    assert not path.exists() and (tmp_path/'supervisor.log.1').read_bytes()==b'x'*11
    supervisor.log('hello',path=path)
    assert path.read_text(encoding='utf-8').endswith(' hello\n')
