"""Bounded caller. Requires an explicitly deployed native-host bridge; no default IPC.

Bridge stdin is one JSON request. preflight returns {create:true,resume:true};
dispatch returns {thread_id,host_ref?}. No retry, absence inference or token output.
"""
from pathlib import Path
import argparse,json,os,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from anime.dispatch_caller import dispatch_once, DispatchFailure
INTENT_MAY_EXIST=False


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('job_id');p.add_argument('--owner',required=True);p.add_argument('--revision',type=int,required=True)
    p.add_argument('--adapter-config',type=Path,required=True,help='Private, owner-controlled JSON argv for a native host bridge')
    p.add_argument('--journal',type=Path,required=True,help='Existing private audit directory')
    a=p.parse_args();command=json.loads(a.adapter_config.read_text(encoding='utf-8-sig'))
    if not isinstance(command,list) or not command or not all(isinstance(v,str) for v in command) or not Path(command[0]).is_absolute():
        raise DispatchFailure('bridge argv requires an absolute executable')
    if not a.journal.is_dir():raise DispatchFailure('private audit directory required')
    class Bridge:
        def call(self,request):
            r=subprocess.run(command,input=json.dumps(request),text=True,encoding='utf-8',capture_output=True,timeout=30,
                             creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if r.returncode:raise DispatchFailure('bridge request failed')
            return json.loads(r.stdout)
        def preflight(self):return self.call({'operation':'preflight'})
        def dispatch(self,**data):return self.call({'operation':'dispatch',**data})
    command_number=[0]
    def cli(*args):
        r=subprocess.run([sys.executable,str(ROOT/'scripts/subtitle_delivery_queue.py'),*args],cwd=ROOT,capture_output=True,
                         env={**os.environ,'PYTHONIOENCODING':'utf-8'},text=True,encoding='utf-8',timeout=30)
        command_number[0]+=1
        for suffix,body in [('stdout',r.stdout),('stderr',r.stderr)]:
            with (a.journal/f'delivery-{command_number[0]:02d}.{suffix}').open('x',encoding='utf-8') as f:f.write(body)
        if r.returncode:raise DispatchFailure('delivery command failed; inspect private audit')
        return json.loads(r.stdout)
    def archive_preflight():
        from scripts.subtitle_delivery_queue import protect_ticket_directory
        folder=ROOT/'data/subtitle-delivery-tickets';folder.mkdir(exist_ok=True)
        protect_ticket_directory(folder)
        # Also validate the caller's output boundary before any persisted intent.
        protect_ticket_directory(a.journal)
        with (a.journal/'caller.jsonl').open('x',encoding='utf-8'):pass
    def journal(row):
        with (a.journal/'caller.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    def confirm(path,thread_id,host_ref):
        args=['confirm',a.job_id,'--ticket',str(path),'--thread-id',thread_id]
        if host_ref:args+=['--host-ref',host_ref]
        r=cli(*args);return r.get('receipt_result',r)
    def begin_controlled():
        global INTENT_MAY_EXIST
        INTENT_MAY_EXIST=True
        return cli('begin',a.job_id,'--owner',a.owner,'--revision',str(a.revision))
    result=dispatch_once(host=Bridge(),archive_preflight=archive_preflight,
        begin=begin_controlled,confirm=confirm,
        interrupted=lambda path,note:cli('interrupted',a.job_id,'--ticket',str(path)),journal=journal)
    # Only fixed public outcome fields; no responsible IDs, prompts or authority.
    print(json.dumps({k:result.get(k) for k in ('receipt_accepted','state_changed','reason','receipt_id')}))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error':type(error).__name__,'reconcile_required':INTENT_MAY_EXIST}))
        raise SystemExit(1)
