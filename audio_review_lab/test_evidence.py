import tempfile
import json
import subprocess
import sys
import unittest
import wave
from fractions import Fraction
from pathlib import Path

from .evidence import audit_wav, map_sample, save_new, validate_observation
from .compare import evaluate_control
from .run import experiment_key, manifest_asset


class EvidenceTests(unittest.TestCase):
    def test_condition_key_ignores_order_but_detects_audio_and_prompt_changes(self):
        req={'adapter':'qwen','model':'local','device':'cpu','dtype':'fp32',
             'cases':[{'wav_sha256':'a','language':'ja'},{'wav_sha256':'b','language':'ja'}]}
        self.assertEqual(experiment_key(req),experiment_key({**req,'cases':list(reversed(req['cases']))}))
        self.assertNotEqual(experiment_key(req),experiment_key({**req,'prompts':{'a':'hiragana only'}}))
        self.assertNotEqual(experiment_key(req),experiment_key({**req,'cases':[
            {**req['cases'][0],'enrollment_sha256':'different_voice'},req['cases'][1]]}))

    def test_nested_model_assets_cannot_escape_or_use_absolute_or_stream_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'model';(root/'ctc').mkdir(parents=True)
            valid=root/'ctc/weights';valid.write_bytes(b'fixture')
            self.assertEqual(manifest_asset(root,'ctc/weights'),valid.resolve())
            for invalid in ('../escape',str(valid.resolve()),'ctc/weights:alternate'):
                with self.subTest(path=invalid),self.assertRaises(ValueError):
                    manifest_asset(root,invalid)
    def runner_fixture(self,root):
        wav=root/'silence.wav'
        with wave.open(str(wav),'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(bytes(32000))
        model=root/'model';model.mkdir()
        cfg={'cases':[{'id':'test','wav':str(wav),'wav_sha256':'0'*64,'language':'ja'}],
             'experiments':{'check':{'adapter':'whisper','python':sys.executable,'model':str(model),
                                    'device':'cpu','dtype':'int8','vad':False}}}
        save_new(root/'config.json',cfg)
        return [sys.executable,'-m','audio_review_lab.run','--config',str(root/'config.json'),
                '--experiment','check','--timeout','10']

    def test_input_mutation_records_failure_and_releases_own_lock(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cmd=self.runner_fixture(root)
            result=subprocess.run(cmd,capture_output=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse((root/'experiment.lock.json').exists())
            outcome=next((root/'runs').glob('*/outcome.json'))
            record=json.loads(outcome.read_bytes())
            self.assertEqual(record['status'],'preflight_failed')
            self.assertFalse(record['acceptance'])

    def test_unknown_lock_is_never_stolen(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cmd=self.runner_fixture(root)
            lock=root/'experiment.lock.json';save_new(lock,{'pid':123,'started_at':0,'experiment':'unknown'})
            original=lock.read_bytes()
            result=subprocess.run(cmd,capture_output=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(lock.read_bytes(),original)
            self.assertFalse((root/'runs').exists())
    def test_control_failure_never_becomes_target_accuracy(self):
        self.assertFalse(evaluate_control('silence','ご視聴ありがとうございました')['passed'])
        self.assertTrue(evaluate_control('silence','')['passed'])
        self.assertIsNone(evaluate_control('C254_reading_number','何人も')['passed'])
    def test_no_acceptance_or_requested_edges_as_observed(self):
        r = dict(status='proposal_only', human_listened=False, current_codex_listened=False,
                 subtitle_acceptance=False, observed_media_span=None,
                 timing_is_estimated=True, scope=['lexical_proposal'])
        validate_observation(r)
        for key,value in [('status','accepted'),('subtitle_acceptance',True),
                          ('observed_media_span',[1,2]),('scope',['speaker_accepted'])]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_observation({**r,key:value})

    def test_piecewise_gaps_and_nonzero_origin(self):
        sections = [{'sample_start':0,'sample_end':16000,'rate':16000,'media_start':'10.5'},
                    {'sample_start':16000,'sample_end':32000,'rate':16000,'media_start':'30'}]
        self.assertEqual(map_sample(8000,sections),Fraction(11))
        self.assertEqual(map_sample(16000,sections),Fraction(30))
        for n in (-1,32000):
            with self.assertRaises(ValueError): map_sample(n,sections)
        for n in (0,1,15999,16000,31999):
            sec=sections[n//16000]
            back=(map_sample(n,sections)-Fraction(sec['media_start']))*sec['rate']+sec['sample_start']
            self.assertEqual(back,n)

    def test_immutable_and_silence_audit(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'a.json'
            save_new(path,{'value':1})
            with self.assertRaises(FileExistsError):save_new(path,{'value':2})
            wav=Path(d)/'a.wav'
            with wave.open(str(wav),'wb') as w:
                w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(bytes(32000))
            audit=audit_wav(wav)
            self.assertEqual(audit['sample_count'],16000)
            self.assertEqual(audit['rms'],0)
            quiet=Path(d)/'quiet.wav'
            with wave.open(str(quiet),'wb') as w:
                w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
                w.writeframes(b'\x01\x00'+bytes(31998))
            self.assertGreater(audit_wav(quiet)['peak'],0)
            self.assertGreater(audit_wav(quiet)['rms'],0)


if __name__=='__main__':unittest.main()
