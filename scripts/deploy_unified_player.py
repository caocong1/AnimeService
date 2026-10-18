"""Legacy entry point: bounded GET-only verification, never deployment/restart.

Backend loading belongs to the owner's separately reviewed supervisor procedure.
Playback acceptance belongs to run_player_qa.py and its synthetic database.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'data/player-qa-readonly')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node.js is required')
    env = {**os.environ, 'PLAYER_QA_BASE': 'http://127.0.0.1:4871',
           'PLAYER_QA_OUT': str(output)}
    with (output/'metadata-verification.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([node, 'scripts/verify_unified_player_live.cjs'],
            cwd=ROOT, env=env, stdout=log, stderr=log, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    (output/'deployment.json').write_text(json.dumps({
        'state': 'metadata_verified' if result.returncode == 0 else 'metadata_check_failed',
        'time': time.time(), 'metadata_exit_code': result.returncode,
        'restart_attempted': False, 'browser_started': False,
        'verification_scope': 'GET-only metadata; no deployment or playback'}, indent=2),
        encoding='utf-8')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
