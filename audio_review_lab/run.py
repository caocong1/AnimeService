"""Bounded serial runs, immutable inputs/results and a conservative owner lock."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .evidence import audit_wav, save_new, sha256


def experiment_key(req):
    condition={k:req.get(k) for k in ('adapter','model','model_manifest_sha256','device','dtype')}
    condition.update(vad=req.get('vad'),zero_pcm_guard=req.get('zero_pcm_guard',False),
                     prompts=req.get('prompts',{}),cases=sorted(
                       [(c['wav_sha256'],c['language'],c.get('enrollment_sha256','')) for c in req['cases']]))
    return hashlib.sha256(json.dumps(condition,sort_keys=True).encode()).hexdigest()


def manifest_asset(model_root, relative):
    root=Path(model_root).resolve(strict=True)
    rel=Path(relative)
    if rel.is_absolute() or '..' in rel.parts or ':' in relative:
        raise ValueError('Unexpected manifest path')
    asset=(root/rel).resolve(strict=True)
    asset.relative_to(root)
    return asset


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--experiment', required=True)
    p.add_argument('--timeout', type=int, default=600)
    p.add_argument('--reason', default='initial_frozen_experiment')
    p.add_argument('--allow-repeat',action='store_true',help='Requires a distinct documented new hypothesis')
    a = p.parse_args()
    if not 1 <= a.timeout <= 1800:
        raise ValueError('A finite, at most 30 minute run is required')
    cfg = json.loads(a.config.read_bytes())
    root = a.config.resolve().parent
    spec = cfg['experiments'][a.experiment]
    keep_lock = False
    lock = root / 'experiment.lock.json'
    # An orphan/unknown owner is reconciled explicitly, never stolen by elapsed time.
    lock_info = {'pid': os.getpid(), 'started_at': time.time(), 'experiment': a.experiment}
    save_new(lock, lock_info)
    run_id = str(uuid.uuid4())
    out = root / 'runs' / run_id
    out.mkdir(parents=True)
    try:
        cases = []
        provenance = []
        for item in cfg['cases']:
            audit = audit_wav(Path(item['wav']))
            if audit['wav_sha256'] != item['wav_sha256']:
                raise ValueError('Frozen case hash mismatch')
            if spec.get('case_ids') and item['id'] not in spec['case_ids']:
                continue
            case={k: item[k] for k in ('id', 'wav', 'wav_sha256', 'language')}
            if spec['adapter']=='ps4':
                enrollment=audit_wav(Path(item['enrollment_wav']))
                if enrollment['wav_sha256']!=item['enrollment_sha256']:
                    raise ValueError('Frozen enrollment changed')
                case.update(enrollment_wav=item['enrollment_wav'],enrollment_sha256=item['enrollment_sha256'])
            cases.append(case)
            provenance.append(item)
        if not cases:
            raise ValueError('No admitted cases')
        if spec.get('case_ids'):
            order={name:i for i,name in enumerate(spec['case_ids'])}
            cases.sort(key=lambda c:order[c['id']])
        # Evaluator/reference labels stay outside the request.
        request = {k: spec[k] for k in ('adapter', 'model', 'device', 'dtype')}
        if spec['adapter'] == 'whisper':
            request['vad'] = spec['vad']
        request['cases'] = cases
        request['case_timeout_seconds'] = spec.get('case_timeout_seconds', 120)
        request['model_manifest_sha256'] = spec.get('model_manifest_sha256')
        request['zero_pcm_guard'] = spec.get('zero_pcm_guard',False)
        request['prompts'] = spec.get('prompts',{})
        key=experiment_key(request)
        for previous in (root/'runs').iterdir():
            if previous==out or not (previous/'outcome.json').exists() or not (previous/'request.json').exists():
                continue
            previous_outcome=json.loads((previous/'outcome.json').read_bytes())
            if previous_outcome['status']=='completed_proposals' and experiment_key(json.loads((previous/'request.json').read_bytes()))==key:
                if not a.allow_repeat:
                    save_new(out/'outcome.json',{'status':'duplicate_experiment_refused',
                               'previous_run_id':previous.name,'acceptance':False})
                    print(json.dumps({'status':'duplicate_experiment_refused','previous_run_id':previous.name}))
                    return
                if a.reason=='initial_frozen_experiment':
                    raise ValueError('Repeat requires a distinct documented hypothesis')
        if spec.get('model_manifest_path'):
            model_manifest_path=Path(spec['model_manifest_path'])
            if sha256(model_manifest_path)!=spec['model_manifest_sha256']:
                raise ValueError('Model manifest changed')
            weight_manifest=json.loads(model_manifest_path.read_bytes())
            for entry in weight_manifest['files']:
                f=manifest_asset(spec['model'],entry['file'])
                if f.stat().st_size!=entry['size'] or sha256(f)!=entry['sha256']:
                    raise ValueError('Model files changed after binding')
        source = out/'source'
        (source/'audio_review_lab').mkdir(parents=True)
        hashes = {}
        for name in ('__init__.py','worker.py','evidence.py','run.py'):
            src = Path(__file__).parent/name
            dest = source/'audio_review_lab'/name
            shutil.copyfile(src,dest)
            hashes[name] = sha256(dest)
        save_new(out / 'request.json', request)
        save_new(out / 'provenance.json', provenance)
        save_new(out / 'manifest.json', {'run_id': run_id, 'experiment': a.experiment,
                 'config_sha256': sha256(a.config), 'request_sha256': sha256(out/'request.json'),
                 'model_manifest_sha256': spec.get('model_manifest_sha256'), 'started_at': time.time(),
                 'implementation_sha256': hashes,
                 'experiment_key':key,
                 'reason': a.reason,
                 'timeout_seconds': a.timeout, 'proposal_only': True, 'acceptance': False})
        if spec['device'] == 'cuda':
            free = int(subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free',
                       '--format=csv,noheader,nounits'], timeout=10).decode().splitlines()[0])
            if free < spec.get('minimum_free_mib', 6000):
                save_new(out/'outcome.json', {'status': 'resource_deferred', 'free_mib': free,
                          'required_mib': spec.get('minimum_free_mib',6000), 'acceptance': False})
                print(json.dumps({'run':run_id,'status':'resource_deferred'}))
                return
        started = time.perf_counter()
        with (out / 'stdout.log').open('xb') as stdout, (out / 'stderr.log').open('xb') as stderr:
            try:
                child = subprocess.Popen([spec['python'], '-m', 'audio_review_lab.worker',
                         '--request', str(out/'request.json'), '--out', str(out)],
                         stdout=stdout, stderr=stderr,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                         cwd=str(source))
                child.wait(timeout=a.timeout)
                status = 'completed_proposals' if child.returncode == 0 else 'runtime_failed'
                code = child.returncode
            except subprocess.TimeoutExpired:
                status, code = 'timeout', None
                # Windows virtualenv launchers have a child interpreter. Kill this
                # experiment's whole tree, never a model service or an unrelated PID.
                try:
                    if os.name == 'nt':
                        killed = subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],
                                                capture_output=True, timeout=15,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                        if killed.returncode != 0 and child.poll() is None:
                            raise RuntimeError('Experiment tree termination unconfirmed')
                    else:
                        child.kill()
                    child.wait(timeout=10)
                except Exception:
                    keep_lock = True
                    status = 'termination_unknown'
        outputs = sorted(x.name for x in out.glob('*.json') if x.name in {c['id']+'.json' for c in cases})
        save_new(out/'outcome.json', {'status': status, 'returncode':code,
                   'wall_seconds':time.perf_counter()-started,'expected_cases':len(cases),
                   'completed_cases':len(outputs),'outputs':outputs,'acceptance':False})
        print(json.dumps({'run_id':run_id,'experiment':a.experiment,'status':status}))
    except Exception as exc:
        if not (out/'outcome.json').exists():
            save_new(out/'outcome.json',{'status':'preflight_failed','error_type':type(exc).__name__,
                                       'error':str(exc)[:300],'acceptance':False})
        raise
    finally:
        if (out/'outcome.json').exists():
            result=json.loads((out/'outcome.json').read_bytes())
            event={'run_id':run_id,'experiment':a.experiment,'status':result['status'],'at':time.time()}
            with (root/'iterations.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(event)+'\n')
        if not keep_lock and json.loads(lock.read_bytes()) == lock_info:
            lock.unlink()


if __name__ == '__main__':
    main()
