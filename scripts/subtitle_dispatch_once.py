"""Bounded caller. Requires an explicitly deployed native-host bridge; no default IPC.

Bridge stdin is one JSON request. preflight returns {create:true,resume:true};
dispatch returns {thread_id,host_ref?}. No retry, absence inference or token output.
"""
from pathlib import Path
from contextlib import ExitStack
import argparse,json,os,subprocess,sys,time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from anime.dispatch_caller import dispatch_once, DispatchFailure
INTENT_MAY_EXIST=False


def run_delivery_cli(args, journal, number, db_path=None, *, host_call_started=False):
    """One direct child with durable byte logs; no PIPE drain, retry or host call.

    Windows venv launchers may leave descendants after the direct child dies.
    Only the child created here can be killed, and reaping it is bounded too.
    """
    if not args:
        raise DispatchFailure('delivery operation required')
    timeout = 120 if args[0] == 'begin' else 30
    operations = {'begin', 'confirm', 'interrupted', 'failed', 'not-created', 'unknown',
                  'status', 'refresh', 'receipts', 'release-lease', 'progress', 'refine',
                  'capacity', 'register-evidence', 'bind-target', 'accepted', 'finished', 'blocked'}
    operation = args[0] if args[0] in operations else 'other'
    started = time.monotonic()
    base = {'schema': 1, 'operation': operation, 'helper_pid': os.getpid(),
            'direct_child_pid': None, 'started_at': time.time(),
            'started_monotonic': started, 'timeout_seconds': timeout,
            # Caller supplies its host history; this helper itself never sends.
            'external_host_call_started': bool(host_call_started),
            'host_called_by_this_helper': False}
    journal = Path(journal)
    streams = {}
    process = None

    def durable(stream, raw):
        remaining = memoryview(raw)
        while remaining:
            written = stream.write(remaining)
            if not written:
                raise OSError('audit write made no progress')
            remaining = remaining[written:]
        stream.flush()
        os.fsync(stream.fileno())

    def event(phase, **extra):
        row = {**base, 'phase': phase, 'at': time.time(),
               'monotonic': time.monotonic(), **extra}
        durable(streams['events.jsonl'], (json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8'))

    def captured():
        sizes = {}
        for name in ('stdout', 'stderr'):
            if name in streams:
                streams[name].flush()
                os.fsync(streams[name].fileno())
                sizes[name + '_bytes'] = os.fstat(streams[name].fileno()).st_size
        return sizes

    def outcome(status, **extra):
        row = {**base, 'status': status, 'elapsed_seconds': round(time.monotonic()-started, 3),
               'direct_reaped': False, 'descendants_unknown': process is not None,
               'reconcile_required': status not in ('audit_ready', 'succeeded'), **extra}
        stream = streams['outcome.json']
        stream.seek(0)
        stream.truncate()
        durable(stream, (json.dumps(row, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

    def reap_direct():
        result = {'direct_reaped': False, 'descendants_unknown': True}
        try:
            process.kill()
            result['direct_kill_requested'] = True
        except OSError as error:
            result.update(direct_kill_requested=False, kill_error_type=type(error).__name__,
                          kill_errno=getattr(error, 'errno', None))
        try:
            result['returncode'] = process.wait(timeout=5)
            result['direct_reaped'] = True
        except (OSError, subprocess.SubprocessError) as error:
            result['reap_error_type'] = type(error).__name__
        return result

    def failure_record(status, error, **extra):
        # Keep an existing audit even if the same filesystem fault now prevents
        # its final update. Never put exception strings/argv/output in metadata.
        fields = {'exception_type': type(error).__name__, 'errno': getattr(error, 'errno', None), **extra}
        try:
            fields['captured_so_far'] = captured()
        except OSError as capture_error:
            fields['capture_error_type'] = type(capture_error).__name__
        if 'outcome.json' in streams:
            try:
                outcome(status, **fields)
            except OSError:
                pass
        if 'events.jsonl' in streams:
            try:
                event(status, **fields)
            except OSError:
                pass

    with ExitStack() as files:
        try:
            # Reserve every audit boundary before launch. A collision never
            # overwrites an earlier operation, and a failed preopen cannot begin.
            for suffix in ('outcome.json', 'events.jsonl', 'stdout', 'stderr'):
                stream = files.enter_context((journal/f'delivery-{number:02d}.{suffix}').open('x+b', buffering=0))
                streams[suffix] = stream
                stream.flush()
                os.fsync(stream.fileno())
            outcome('audit_ready')
            event('audit_ready')
            outcome('launching')
            event('launching')
        except OSError as error:
            failure_record('audit_preopen_failed', error, reconcile_required=bool(host_call_started))
            raise DispatchFailure('delivery audit unavailable; command not started') from None
        command = [sys.executable, str(ROOT/'scripts/subtitle_delivery_queue.py')]
        if db_path is not None:
            command += ['--db', str(db_path)]
        command += list(args)
        try:
            process = subprocess.Popen(
                command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=streams['stdout'], stderr=streams['stderr'],
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError) as error:
            failure_record('launch_failed', error)
            raise DispatchFailure('delivery command launch failed; inspect private audit') from None
        base['direct_child_pid'] = process.pid
        try:
            outcome('running')
            event('started')
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            reaped = reap_direct()
            failure_record('timed_out', error, partial_output=True, reconcile_required=True, **reaped)
            raise DispatchFailure('delivery command timed out; partial private audit preserved; no retry') from None
        except Exception as error:
            reaped = reap_direct()
            failure_record('controller_failed', error, reconcile_required=True, **reaped)
            raise DispatchFailure('delivery controller failed; inspect private audit; no retry') from None
        if code:
            failure_record('process_exit', subprocess.CalledProcessError(code, []),
                           returncode=code, direct_reaped=True)
            raise DispatchFailure('delivery command failed; inspect private audit')
        try:
            sizes = captured()
            # Do not move an inherited writer's file offset or follow a growing
            # file indefinitely if descendants outlive the successful child.
            with (journal/f'delivery-{number:02d}.stdout').open('rb') as reader:
                before_size = os.fstat(reader.fileno()).st_size
                raw = reader.read(sizes['stdout_bytes'])
                after_size = os.fstat(reader.fileno()).st_size
            if before_size != sizes['stdout_bytes'] or after_size != before_size or len(raw) != before_size:
                raise ValueError('stdout snapshot incomplete or changed')
            result = json.loads(raw.decode('utf-8'))
        except (OSError, ValueError, UnicodeError) as error:
            failure_record('invalid_output', error, returncode=code, direct_reaped=True)
            raise DispatchFailure('delivery command returned invalid JSON; inspect private audit') from None
        try:
            outcome('succeeded', returncode=code, direct_reaped=True, captured_so_far=sizes)
            event('succeeded', returncode=code, direct_reaped=True, captured_so_far=sizes)
        except OSError as error:
            failure_record('audit_finalize_failed', error, returncode=code, direct_reaped=True)
            raise DispatchFailure('delivery result audit unavailable; inspect private audit') from None
        return result


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
        command_number[0]+=1
        return run_delivery_cli(args, a.journal, command_number[0],
                                host_call_started=args[0] in ('confirm', 'interrupted'))
    def archive_preflight():
        from scripts.subtitle_delivery_queue import protect_ticket_directory, preflight_ticket_archive
        preflight_ticket_archive(ROOT/'data/anime.db')
        # Also validate the caller's output boundary before any persisted intent.
        protect_ticket_directory(a.journal)
        with (a.journal/'caller.jsonl').open('x',encoding='utf-8'):pass
    def journal(row):
        with (a.journal/'caller.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
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
