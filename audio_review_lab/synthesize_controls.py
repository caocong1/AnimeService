"""Local Japanese synthetic diagnostics; never natural-speech ground truth."""
import argparse
import importlib.metadata
import json
from pathlib import Path

from .evidence import audit_wav, save_new


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    import numpy as np
    import pyopenjtalk
    from scipy.signal import resample_poly
    import soundfile as sf
    texts=[
        '赤いドアを開けてください。','赤いドアを開けないでください。',
        '三人が外で待っています。','十人が外で待っています。',
        'レベル百に到達しました。','レベル千に到達しました。',
        'わたしは彼を倒しました。','彼はわたしを倒しました。',
        'この言葉を聞いたことがあります。','この言葉を聞いたことがありません。',
        'ナンニンモ参加しました。','ナンビトモ立ち入れません。']
    a.out.mkdir(parents=True,exist_ok=False)
    inputs=a.out/'inputs';inputs.mkdir()
    recipe=[];cases=[];signals=[]
    for i,text in enumerate(texts):
        audio,rate=pyopenjtalk.tts(text,run_marine=False)
        # OpenJTalk produces float sample values in PCM16 units, not [-1,1].
        audio=np.asarray(audio,dtype=np.float64)/32768
        audio=resample_poly(audio,1,int(rate)//16000)
        audio=np.pad(audio,(8000,8000))
        if not np.isfinite(audio).all() or not np.max(np.abs(audio))>0:
            raise ValueError('Invalid synthesized samples')
        # Resampling may overshoot; apply a recorded uniform gain, never clip.
        gain=min(1.0,0.8/float(np.max(np.abs(audio))))
        audio=audio*gain
        signals.append(audio)
        name=f's{i:02d}';wav=inputs/(name+'.wav');sf.write(str(wav),audio,16000,subtype='PCM_16')
        audit=audit_wav(wav)
        cases.append({'id':name,'wav':str(wav.resolve()),'wav_sha256':audit['wav_sha256'],
                      'language':'ja','role':'synthetic_japanese_diagnostic',
                      'issue_id':name,'audio':audit})
        recipe.append({'id':name,'intended_text':text,'intended_kana':pyopenjtalk.g2p(text,kana=True),
                       'gain':gain,'resampling':'scipy resample_poly 48k to 16k',
                       'gold_status':'intended_synthetic_script_not_independent_human_transcript',
                       'human_listened':False,'natural_speech_accuracy':None})
    # Two synthetic streams; pitch shift does not establish natural speaker identities.
    pair=[signals[0],signals[3]]
    size=max(len(x) for x in pair)
    mix=sum(np.pad(x,(0,size-len(x))) for x in pair)*0.5
    wav=inputs/'s12.wav';sf.write(str(wav),mix,16000,subtype='PCM_16')
    audit=audit_wav(wav)
    cases.append({'id':'s12','wav':str(wav.resolve()),'wav_sha256':audit['wav_sha256'],
                  'language':'ja','role':'synthetic_same_voice_two_stream_overlap',
                  'issue_id':'s12','audio':audit})
    recipe.append({'id':'s12','intended_streams':[texts[0],texts[3]],
                   'speaker_identity_gold':None,'human_listened':False})
    save_new(a.out/'cases.json',cases)
    save_new(a.out/'evaluator-only-recipe.json',{'source':'offline OpenJTalk bundled dictionary/voice',
             'version':importlib.metadata.version('pyopenjtalk-plus'),'cases':recipe,
             'natural_animation_gold':False,'private_audio_uploaded':False})
    print(json.dumps({'synthetic_cases':len(cases),'natural_animation_gold':False}))


if __name__=='__main__':main()
