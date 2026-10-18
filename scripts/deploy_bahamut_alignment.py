"""Bounded GET-only capability check; backend reloads use the owner procedure."""
import argparse
import json
import time
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data/diagnostics/bahamut-alignment-readonly'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout',type=int,default=5)
    parser.add_argument('--output',type=Path,default=OUT)
    args=parser.parse_args()
    if not 1<=args.timeout<=30:parser.error('--timeout must be between 1 and 30 seconds')
    args.output.mkdir(parents=True,exist_ok=True)
    proof={'time':time.time(),'restart_attempted':False,'browser_started':False,
           'scope':'GET-only alignment capability; no deployment or playback'}
    try:
        with requests.Session() as session:
            session.trust_env=False
            response=session.get('http://127.0.0.1:4871/api/web/alignment-capabilities',timeout=args.timeout,allow_redirects=False)
            proof['http_status']=response.status_code
            if response.status_code==404:ready=False
            else:
                response.raise_for_status()
                if response.status_code!=200:raise ValueError('Unexpected capability status')
                ready=response.json().get('bahamut_reference')=='bounded-hls-v1'
        proof['state']='capability_verified' if ready else 'owner_reload_required'
        result=0 if ready else 2
    except (requests.RequestException,ValueError) as error:
        proof.update(state='metadata_check_failed',error_type=type(error).__name__)
        result=1
    (args.output/'deployment.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':raise SystemExit(main())
