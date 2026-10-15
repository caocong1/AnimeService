"""Bounded linear stereo-mixing hypothesis; never a speaker acceptance tool."""
from __future__ import annotations

import argparse
import importlib.metadata as metadata
import itertools
import json
import os
import threading
import time
import warnings
from pathlib import Path

from .evidence import audit_wav, save_new, sha256


def main():
    p = argparse.ArgumentParser()
    for name in ('control-a', 'control-b', 'target-stereo', 'target-provenance', 'out', 'lock'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    save_new(a.lock, {'pid': os.getpid(), 'experiment': 'stereo_ica_linear_mixing',
                      'started_at': time.time()})
    try:
        a.out.mkdir(parents=True, exist_ok=False)
        with (a.out/'implementation.py').open('xb') as f:
            f.write(Path(__file__).read_bytes())
    except Exception:
        if a.lock.exists() and json.loads(a.lock.read_bytes())['pid']==os.getpid():
            a.lock.unlink()
        raise
    def expired():
        if not (a.out/'outcome.json').exists():
            save_new(a.out/'outcome.json',{'status':'timeout','acceptance':False})
        os._exit(124)
    deadline = threading.Timer(120, expired)
    deadline.daemon = True
    deadline.start()
    started = time.perf_counter()
    try:
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        from sklearn.decomposition import FastICA

        def separate(x, prefix):
            alg = FastICA(n_components=2, algorithm='parallel', whiten='unit-variance',
                          fun='logcosh', max_iter=1000, tol=1e-6, random_state=0)
            with warnings.catch_warnings(record=True) as caught:
                s = alg.fit_transform(x)
            rebuilt = alg.inverse_transform(s)
            error = float(np.linalg.norm(rebuilt-x)/np.linalg.norm(x))
            if not np.isfinite(s).all() or error > 1e-8:
                raise ValueError('Nonfinite source or reconstruction failure')
            slots, rows = [], []
            for i in range(2):
                mix = alg.mixing_[:, i]
                sign = np.sign(mix[np.argmax(np.abs(mix))]) or 1
                signal = s[:, i]*float(np.linalg.norm(mix)/np.sqrt(2))*sign
                gain = min(1.0, 0.99/max(float(np.max(np.abs(signal))), 1e-12))
                dest = a.out/(prefix+f'-slot{i}.wav')
                sf.write(str(dest), signal*gain, 16000, subtype='PCM_16')
                slots.append(signal)
                rows.append({'id':prefix+f'{i}', 'wav':str(dest.resolve()),
                             'wav_sha256':sha256(dest), 'audio':audit_wav(dest),
                             'gain':gain, 'mixing_vector':mix.tolist(),
                             'output_scale':'RMS norm of original channel contribution; no equal-RMS amplification'})
            return slots, {'slots':rows, 'iterations':int(alg.n_iter_),
                           'warnings':[str(w.message) for w in caught],
                           'mixing':alg.mixing_.tolist(), 'unmixing':alg.components_.tolist(),
                           'channel_mean':alg.mean_.tolist(), 'relative_reconstruction_error':error}

        def sisdr(est, ref):
            est, ref = est-est.mean(), ref-ref.mean()
            target = np.dot(est,ref)/max(np.dot(ref,ref),1e-20)*ref
            return float(10*np.log10(max(np.dot(target,target),1e-20)/max(np.dot(est-target,est-target),1e-20)))

        controls = []
        for path in (a.control_a,a.control_b):
            audit_wav(path)
            y, sr = sf.read(str(path), dtype='float64')
            controls.append(y)
        n=max(map(len,controls))
        refs=[np.pad(y,(0,n-len(y))) for y in controls]
        matrix=np.array([[1.0,0.4],[0.4,1.0]])
        stereo=np.stack(refs,axis=1)@matrix.T
        recovered, control = separate(stereo,'ic')
        permutations = [(perm,[sisdr(recovered[perm[i]],refs[i]) for i in range(2)])
                        for perm in itertools.permutations(range(2))]
        best=max(permutations,key=lambda v:min(v[1]))
        control.update(intended_mixing=matrix.tolist(),permutation=list(best[0]),
                       synthetic_sisdr_db=best[1], natural_gold=False,
                       source_sha256=[sha256(a.control_a),sha256(a.control_b)])
        save_new(a.out/'control.json',control)
        if min(best[1]) < 10 or control['warnings']:
            save_new(a.out/'outcome.json',{'status':'synthetic_control_failed_target_not_processed',
                      'acceptance':False,'wall_seconds':time.perf_counter()-started})
            return
        provenance=json.loads(a.target_provenance.read_bytes())
        if sha256(a.target_stereo)!=provenance['wav_sha256']:
            raise ValueError('Target stereo changed')
        target,sr=sf.read(str(a.target_stereo),dtype='float64',always_2d=True)
        if sr!=48000 or target.shape[1]!=2 or not 0<len(target)/sr<=30:
            raise ValueError('Target must be short frozen 48k stereo')
        target=resample_poly(target,1,3,axis=0)
        _, result=separate(target,'it')
        result.update(status='proposal_only',scope=['separation_derivative'],
                      target_provenance=provenance, observed_media_span=None,
                      speaker_count_accepted=False, source_slots=2,
                      source_sha256=sha256(a.target_stereo), resample=[48000,16000],
                      human_listened=False,current_codex_listened=False,acceptance=False,
                      hypothesis='Two spatially distinct instantaneous linear mixtures; soundtracks may violate this assumption',
                      parameters={'whiten':'unit-variance','seed':0,'tol':1e-6,'max_iter':1000},
                      implementation_sha256=sha256(Path(__file__)),
                      versions={k:metadata.version(k) for k in ('numpy','scipy','scikit-learn','soundfile')})
        save_new(a.out/'target.json',result)
        save_new(a.out/'outcome.json',{'status':'completed_derivative_proposals',
                  'acceptance':False,'wall_seconds':time.perf_counter()-started})
    except Exception as exc:
        save_new(a.out/'outcome.json',{'status':'failed','error':str(exc)[:300],'acceptance':False})
        raise
    finally:
        deadline.cancel()
        if a.lock.exists() and json.loads(a.lock.read_bytes())['pid']==os.getpid():
            a.lock.unlink()


if __name__ == '__main__':
    main()
