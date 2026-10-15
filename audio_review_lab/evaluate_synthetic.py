"""Script consistency diagnostics; does not score natural Japanese accuracy."""
import argparse
import json
import re
from pathlib import Path

from .evidence import save_new, sha256


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--recipe',type=Path,required=True)
    p.add_argument('--runs',nargs='+',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    import pyopenjtalk
    recipe=json.loads(a.recipe.read_bytes())
    def kana(s):return re.sub(r'[^ァ-ヺー]', '',pyopenjtalk.g2p(s,kana=True))
    rows=[]
    for case in recipe['cases']:
        results=[]
        for run in a.runs:
            path=run/(case['id']+'.json')
            if not path.exists():
                results.append({'run_id':run.name,'missing':True});continue
            result=json.loads(path.read_bytes())
            text=result['text_hypothesis']
            score={'run_id':run.name,'result_sha256':sha256(path),'text':text}
            if case['id']=='s12':
                score['criterion']='synthetic_two_stream_content_recall_only'
                score['intended_streams_present']=[kana(t) in kana(text) for t in case['intended_streams']]
            elif case['id'] in ('s10','s11'):
                score['criterion']='same_kanji_transcript_cannot_establish_actual_reading'
                score['passed']=None
            else:
                score['criterion']='intended_script_consistency_with_dictionary_normalization'
                score['passed']=kana(text)==kana(case['intended_text'])
                if case['id']=='s05':
                    score['numeric_semantics_present']=bool(re.search(r'千|1000|１０００|せん|セン',text))
                    score['passed']=score['passed'] and score['numeric_semantics_present']
            results.append(score)
        rows.append({'case_id':case['id'],'results':results})
    save_new(a.out,{'scope':'synthetic_diagnostic_only','natural_animation_accuracy':None,
                   'human_listened':False,'acceptance':False,'rows':rows})
    print(json.dumps({'diagnostic_cases':len(rows),'natural_accuracy':None}))


if __name__=='__main__':main()
