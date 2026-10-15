"""One official, checksummed local music-removal experiment, without acceptance."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import time
import urllib.request
from fractions import Fraction
from .evidence import save_new, sha256, audit_wav


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--acquire-only',action='store_true')
    a=p.parse_args()
    root=Path('data/subtitle-studio/audio-review-lab').resolve()
    modeldir=root/'models/demucs-official'
    modeldir.mkdir(exist_ok=True)
    weights=modeldir/'955717e8-8726e21a.th'
    url='https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th'
    if not weights.exists():
        part=weights.with_suffix('.partial')
        with urllib.request.urlopen(url,timeout=60) as response,part.open('xb') as target:
            while block:=response.read(1024*1024):
                target.write(block)
        digest=sha256(part)
        if not digest.startswith('8726e21a'):
            raise ValueError('Official filename checksum mismatch')
        part.rename(weights)
        save_new(modeldir/'manifest.json',{'official_url':url,'sha256':digest,
                 'size':weights.stat().st_size,'official_checksum_prefix':'8726e21a'})
    manifest=json.loads((modeldir/'manifest.json').read_bytes())
    if sha256(weights)!=manifest['sha256']:
        raise ValueError('Local model changed')
    if a.acquire_only:
        print(json.dumps(manifest));return
    import torch
    import numpy as np
    import soundfile as sf
    import julius
    from demucs.htdemucs import HTDemucs
    from demucs.apply import apply_model
    import importlib.metadata as md
    torch.set_num_threads(6)
    # Permit only the official architecture class, not arbitrary pickle globals.
    with torch.serialization.safe_globals([HTDemucs, Fraction]):
        package=torch.load(weights,map_location='cpu',weights_only=True)
    if package['klass'] is not HTDemucs:
        raise ValueError('Unexpected checkpoint architecture')
    model=HTDemucs(*package['args'],**package['kwargs'])
    model.load_state_dict(package['state'],strict=True)
    model.eval()
    out=root/'demucs-probe-v1'
    out.mkdir(exist_ok=False)
    source=json.loads((root/'focus-continuation-20261013/cases.json').read_bytes())[0]
    command=source['decode_command'];media=command[command.index('-i')+1]
    cases=[]
    for name,start,end in [('d00',48,56),('d01',817,825),('d02',1064.3,1067.3),('d03',1110,1114)]:
        stereo=out/(name+'-original.wav')
        args=[command[0],'-nostdin','-v','info','-ss',str(start),'-i',media,
              '-map','0:1','-t',str(end-start),'-ar',str(model.samplerate),
              '-ac','2','-c:a','pcm_s16le',str(stereo)]
        result=subprocess.run(args,capture_output=True,timeout=60)
        (out/(name+'-decode.log')).write_bytes(result.stderr);result.check_returncode()
        samples,sr=sf.read(stereo,dtype='float32',always_2d=True)
        audio=torch.from_numpy(samples.T).unsqueeze(0)
        started=time.perf_counter()
        # No random-shift averaging and no time stretch. Exact output count checked.
        with torch.inference_mode():
            stems=apply_model(model,audio,device='cpu',shifts=0,split=True,
                              overlap=0.25,progress=False,num_workers=0)
        assert stems.shape[-1]==audio.shape[-1]
        vocals=stems[0,model.sources.index('vocals')]
        mono=julius.resample_frac(vocals.mean(dim=0),sr,16000).numpy()
        peak=float(np.max(np.abs(mono)));gain=min(1.,0.98/peak) if peak else 1.
        wav=out/(name+'.wav');sf.write(wav,mono*gain,16000,subtype='PCM_16')
        case={'id':name,'wav':str(wav),'wav_sha256':sha256(wav),'language':'ja',
              'role':'target_development_music_removal','audio':audit_wav(wav),
              'requested_media_span':[start,end],'observed_media_span':None,
              'source_media_sha256':source['source_media_sha256'],
              'parent_stereo_sha256':sha256(stereo),'checkpoint_sha256':manifest['sha256'],
              'transform':'HTDemucs vocals, stereo mean, Julius resample',
              'model_sources':model.sources,'model_sample_rate':sr,
              'gain':gain,'output_length_checked':True,'speech_latency_measured':False,
              'speaker_isolation_claim':False,'acceptance':False,
              'wall_seconds':time.perf_counter()-started}
        cases.append(case)
        save_new(out/(name+'-derivative.json'),case)
        print(json.dumps({'case_id':name,'status':'derivative_only'}),flush=True)
    save_new(out/'cases.json',cases)
    versions={k:md.version(k) for k in ('torch','torchaudio','demucs','julius')}
    save_new(out/'versions.json',versions)
    cfg=json.loads((root/'config-v9.json').read_bytes());cfg['cases'].extend(cases)
    for adapter,base in [('reazon','reazon_context_and_repeat_call'),
                         ('anime_whisper','anime_whisper_context_and_repeat_call')]:
        spec=dict(cfg['experiments'][base]);spec['case_ids']=[c['id'] for c in cases]
        cfg['experiments'][adapter+'_demucs_probe']=spec
    save_new(root/'config-v10.json',cfg)


if __name__=='__main__':
    main()
