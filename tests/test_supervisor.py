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


def test_local_probe_ignores_outbound_proxies_and_application_degradation(monkeypatch):
    opened=Mock()
    opened.open.side_effect=lambda *a,**k:io.BytesIO(b'{"service":"AnimeService","ok":false}')
    builder=Mock(return_value=opened)
    monkeypatch.setattr(supervisor.urllib.request,'build_opener',builder)
    monkeypatch.setenv('HTTP_PROXY','http://unreachable.invalid:8080')
    assert supervisor.probe('http://127.0.0.1:4871')=='ours'
    assert builder.call_args.args[0].proxies=={}


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


def test_probe_reports_a_dead_worker_only_when_health_says_so():
    assert supervisor.probe('http://x',opener_for({'health':{'service':'AnimeService','worker_thread':False}}))=='stalled'
    assert supervisor.probe('http://x',opener_for({'health':{'service':'AnimeService','worker_thread':True}}))=='ours'
    old=urllib.error.HTTPError('u',404,'nf',{},None)  # /api/status carries no worker_thread
    assert supervisor.probe('http://x',opener_for({'health':old,'status':{'service':'AnimeService'}}))=='ours'


def test_dead_worker_after_the_startup_grace_counts_as_hung(tmp_path):
    clock=Clock();child=Child();child.clock=clock;terminate=Mock()
    outcome=supervisor.watch(child,'http://x',stop=tmp_path/'stop.request',prober=lambda base:'stalled',sleep=clock.sleep,
                             now=clock.time,terminate=terminate,worker_grace=180,worker=True)
    # Probes inside the grace are healthy; six consecutive stalled probes after it are a hang.
    assert outcome=='hung' and terminate.call_count==1 and 180+5*30<=clock.now-1000<=180+7*30


def test_dead_worker_is_tolerated_when_the_worker_is_disabled_or_recovers(tmp_path):
    clock=Clock();child=Child(exit_at=1000+3600);child.clock=clock;terminate=Mock()
    assert supervisor.watch(child,'http://x',stop=tmp_path/'stop.request',prober=lambda base:'stalled',sleep=clock.sleep,
                            now=clock.time,terminate=terminate,worker=False)=='exited'
    clock=Clock();child=Child(exit_at=1000+3600);child.clock=clock
    probes=iter((['stalled']*5+['ours'])*100)
    assert supervisor.watch(child,'http://x',stop=tmp_path/'stop.request',prober=lambda base:next(probes),sleep=clock.sleep,
                            now=clock.time,terminate=terminate,worker_grace=0,worker=True)=='exited'
    assert terminate.call_count==0


def test_supervisor_survives_failed_launches_and_a_locked_log(tmp_path,monkeypatch):
    monkeypatch.setattr(supervisor,'ROOT',tmp_path);monkeypatch.setattr(supervisor,'STOP',tmp_path/'data/stop.request')
    monkeypatch.setattr(supervisor,'acquire_mutex',lambda:1);monkeypatch.setattr(supervisor,'probe',lambda base:'down')
    monkeypatch.setattr(supervisor.time,'sleep',lambda seconds:None)
    child=Mock(pid=4242,returncode=0)
    popen=Mock(side_effect=[FileNotFoundError('pythonw.exe'),FileNotFoundError('pythonw.exe'),child])
    monkeypatch.setattr(supervisor.subprocess,'Popen',popen);monkeypatch.setattr(supervisor,'watch',lambda process,base:'stopped')
    assert supervisor.main()==0 and popen.call_count==3
    text=(tmp_path/'logs/supervisor.log').read_text(encoding='utf-8')
    assert text.count('supervisor iteration failed FileNotFoundError')==2 and 'retry in 5s' in text and 'retry in 10s' in text
    assert 'anime.run pid=4242 stopped' in text
    directory=tmp_path/'locked.log';directory.mkdir()
    supervisor.log('hello',path=directory)  # opening a directory raises; the log write is best-effort


def test_start_marker_separates_children_in_process_log(tmp_path):
    path=tmp_path/'process.log'
    with path.open('a',encoding='utf-8') as out:
        out.write('previous child output\n');supervisor.start_marker(out)
    last=path.read_text(encoding='utf-8').splitlines()[-1]
    assert last.endswith(f' --- start anime.run (supervisor pid={supervisor.os.getpid()}) ---') and last[:4].isdigit()
