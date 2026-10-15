"""Create reversible stereo-channel probes; neither isolate nor certify a speaker."""
from pathlib import Path
import json
import subprocess
import copy

from .evidence import audit_wav, save_new, sha256


def main():
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    root = Path('data/subtitle-studio/audio-review-lab').resolve()
    cfg = json.loads((root/'config-v8.json').read_bytes())
    source = json.loads((root/'focus-continuation-20261013/cases.json').read_bytes())[0]
    command = source['decode_command']
    media = command[command.index('-i')+1]
    ffmpeg = command[0]
    out = root/'channel-probe-v1'
    out.mkdir(exist_ok=False)
    cases = []
    for group, start, end in [('o',51.9,54.6), ('f',1064.8,1066.8),
                               ('r',1110.9,1113.2), ('n',818.2,823.6)]:
        stereo = out/(group+'-source.wav')
        args = [ffmpeg,'-nostdin','-v','info','-ss',str(start),'-i',media,
                '-map','0:1','-t',str(end-start),'-af','ashowinfo',
                '-ar','48000','-ac','2','-c:a','pcm_s16le',str(stereo)]
        result = subprocess.run(args, capture_output=True, timeout=60)
        (out/(group+'-decode.log')).write_bytes(result.stderr)
        result.check_returncode()
        audio, sr = sf.read(stereo, dtype='float64', always_2d=True)
        assert sr == 48000 and audio.shape[1] == 2
        transforms = {'L': audio[:,0], 'R': audio[:,1],
                      'M': (audio[:,0]+audio[:,1])/2,
                      'S': (audio[:,0]-audio[:,1])/2}
        for kind, samples in transforms.items():
            wav = out/(group+kind+'.wav')
            samples = resample_poly(samples,1,3)
            peak = float(np.max(np.abs(samples)))
            gain = min(1.,0.98/peak) if peak else 1.
            sf.write(wav,samples*gain,16000,subtype='PCM_16')
            case = {'id':group+kind,'wav':str(wav),'wav_sha256':sha256(wav),
                    'language':'ja','role':'target_development_channel_probe',
                    'issue_id':group,'audio':audit_wav(wav),
                    'source_media_sha256':source['source_media_sha256'],
                    'audio_stream':1,'requested_media_span':[start,end],
                    'observed_media_span':None,'parent_stereo_sha256':sha256(stereo),
                    'channel_transform':kind,'gain':gain,
                    'source_decode_command':args,'sample_rate_conversion':[48000,16000],
                    'speaker_isolation_claim':False,'acceptance':False}
            cases.append(case)
    save_new(out/'cases.json',cases)
    cfg['cases'].extend(cases)
    for adapter, base in [('reazon','reazon_context_and_repeat_call'),
                          ('anime_whisper','anime_whisper_context_and_repeat_call')]:
        spec = copy.deepcopy(cfg['experiments'][base])
        spec['case_ids']=[c['id'] for c in cases]
        cfg['experiments'][adapter+'_channel_probe']=spec
    save_new(root/'config-v9.json',cfg)
    print(json.dumps({'cases':len(cases),'config':'config-v9.json'}))


if __name__ == '__main__':
    main()
