"""Compare frozen runs without treating CC/ASR as Japanese ground truth."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .evidence import save_new, sha256


def normalize_control(s):
    return re.sub(r'[^a-z0-9]', '', s.lower().replace('2', 'two').replace('3', 'three'))


def evaluate_control(issue, text):
    if issue == 'silence':
        return {'criterion': 'zero_PCM_has_no_words', 'passed': not text.strip()}
    expected = {
        'english_positive': 'The green paper is under the table. There are two cups.',
        'english_negative': 'Do not open the red door. Three people are waiting outside.'
    }
    if issue in expected:
        return {'criterion': 'synthetic_English_input_control_only',
                'passed': normalize_control(text) == normalize_control(expected[issue])}
    return {'criterion': 'target_Japanese_ground_truth_not_available', 'passed': None}


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--runs',nargs='+',required=True)
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    cfg=json.loads(a.config.read_bytes())
    root=a.config.resolve().parent
    rows=[]
    for c in cfg['cases']:
        outputs=[]
        for run_id in a.runs:
            run=root/'runs'/run_id
            result=run/(c['id']+'.json')
            if not result.exists():
                outputs.append({'run_id':run_id,'missing':True})
                continue
            r=json.loads(result.read_bytes())
            if r['audio']['wav_sha256']!=c['wav_sha256']:
                raise ValueError('Cannot compare different inputs as a matched pair')
            outputs.append({'run_id':run_id,'text':r['text_hypothesis'],
                            'wall_seconds':r['wall_seconds'],'result_sha256':sha256(result),
                            'control':evaluate_control(c['issue_id'],r['text_hypothesis']),
                            'truncated_or_uncertain':r.get('hit_token_limit',False) or
                               r.get('finish_reason')=='limit_or_other'})
        rows.append({'case_id':c['id'],'issue_id':c['issue_id'],'role':c['role'],
                     'outputs':outputs,'lexical_decision':'open','speaker_decision':'open',
                     'timing_decision':'open'})
    save_new(a.out,{'scope':'experiment_comparison_only','japanese_accuracy':None,
                   'whole_episode_accepted':False,'rows':rows,'runs':a.runs})
    print(json.dumps({'compared_cases':len(rows),'acceptance':False}))


if __name__=='__main__':main()
