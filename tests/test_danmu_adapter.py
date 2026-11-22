"""Synthetic danmu adapter supervision: fake node and processes only; the real adapter never starts."""
import os,subprocess
from types import SimpleNamespace

import psutil,pytest

from anime import log_files,webplayer


class Stop:
    """Allows a fixed number of loop iterations."""
    def __init__(self,iterations=1):self.left=iterations
    def is_set(self):return self.left<=0
    def wait(self,seconds):self.left-=1


@pytest.fixture
def adapter(tmp_path,monkeypatch):
    for d in ('vendor/danmuapi/node_modules','data','logs'):(tmp_path/d).mkdir(parents=True)
    monkeypatch.setattr(webplayer,'ROOT',tmp_path)
    monkeypatch.setattr(webplayer.shutil,'which',lambda name:'/synthetic/node')
    def no_process(pid):raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil,'Process',no_process)
    spawned=[]
    def popen(args,**kw):
        spawned.append((args,kw));return SimpleNamespace(pid=5151,poll=lambda:0)
    monkeypatch.setattr(webplayer.subprocess,'Popen',popen)
    monkeypatch.delenv('LOG_LEVEL',raising=False)
    return SimpleNamespace(root=tmp_path,log=tmp_path/'logs/danmu-start.log',spawned=spawned)


def oversized(path,size=20*1024*1024):
    with path.open('wb') as f:
        f.truncate(size);f.seek(-5,os.SEEK_END);f.write(b'TAIL\n')


def test_spawn_bounds_the_log_and_drops_stdout_chatter(adapter):
    oversized(adapter.log)
    webplayer.maintain_danmu(Stop())
    previous=adapter.log.with_name('danmu-start.log.1')
    assert previous.stat().st_size==log_files.KEEP and previous.read_bytes().endswith(b'TAIL\n')
    (args,kw),=adapter.spawned
    assert args==['/synthetic/node','danmu_api/server.js']
    assert kw['stdout'] is subprocess.DEVNULL and kw['stderr'].name==str(adapter.log) and kw['stderr'].mode=='ab'
    assert kw['env']['LOG_LEVEL']=='warn' and kw['env']['PATH']==os.environ['PATH']
    assert (adapter.root/'data/danmu.pid').read_text()=='5151'


def test_an_explicit_log_level_is_kept(adapter,monkeypatch):
    monkeypatch.setenv('LOG_LEVEL','error')
    webplayer.maintain_danmu(Stop())
    assert adapter.spawned[0][1]['env']['LOG_LEVEL']=='error'


def test_adopted_adapter_is_left_alone(adapter,monkeypatch):
    oversized(adapter.log)
    (adapter.root/'data/danmu.pid').write_text('4242')
    repo=adapter.root/'vendor/danmuapi'
    monkeypatch.setattr(psutil,'Process',lambda pid:SimpleNamespace(cwd=lambda:str(repo),cmdline=lambda:['node','danmu_api/server.js']))
    webplayer.maintain_danmu(Stop())
    assert adapter.spawned==[] and adapter.log.stat().st_size==20*1024*1024
    assert not adapter.log.with_name('danmu-start.log.1').exists()


def test_a_held_log_does_not_block_the_spawn(adapter,monkeypatch):
    oversized(adapter.log)
    original=type(adapter.log).unlink
    def held(path,*a,**k):
        if path==adapter.log:raise PermissionError(32,'being used by another process')
        return original(path,*a,**k)
    monkeypatch.setattr(type(adapter.log),'unlink',held)
    webplayer.maintain_danmu(Stop())
    assert len(adapter.spawned)==1 and adapter.log.exists()


def test_bound_log_limits(tmp_path):
    log=tmp_path/'child.log'
    assert not log_files.bound_log(log)                       # missing
    log.write_bytes(b'small');assert not log_files.bound_log(log) and log.read_bytes()==b'small'
    oversized(log,9*1024*1024);assert log_files.bound_log(log)
    previous=log.with_name('child.log.1')
    assert not log.exists() and previous.stat().st_size==log_files.KEEP
    log.write_bytes(b'x'*100);assert log_files.bound_log(log,limit=10,keep=1000) and previous.read_bytes()==b'x'*100
