"""Authorized public weights only; no private media, inference or credentials."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .evidence import save_new, sha256


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--repo', required=True)
    p.add_argument('--revision', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--allow',nargs='+',help='Explicit file allowlist for this official snapshot')
    a = p.parse_args()
    if not a.repo.startswith(('Qwen/', 'google/')) and a.repo not in (
            'alibabasglab/MossFormer2_SS_16K','litagin/anime-whisper',
            'reazon-research/reazonspeech-k2-v2','hshispeech/Ruby-ASR-1.7B',
            'TaurenMountain/PS4'):
        raise ValueError('Explicit official repo required')
    if len(a.revision) != 40:
        raise ValueError('Explicit official repo and immutable revision required')
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '1'
    os.environ['HF_HUB_ETAG_TIMEOUT'] = '15'
    os.environ['HF_HUB_DOWNLOAD_TIMEOUT'] = '60'
    os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
    from huggingface_hub import snapshot_download
    dest = snapshot_download(a.repo, revision=a.revision, local_dir=str(a.out), token=False,
                            allow_patterns=a.allow or ['*.json', '*.safetensors', '*.jinja', '*.model', '*.txt', 'README.md',
                                            '*.pt','*.onnx','last_best_checkpoint'],
                            max_workers=2, etag_timeout=15)
    files = []
    for f in sorted(Path(dest).rglob('*')):
        if f.is_file() and '.cache' not in f.relative_to(dest).parts and f.name != 'weight-manifest.json':
            files.append({'file': f.relative_to(dest).as_posix(), 'size': f.stat().st_size,
                          'sha256': sha256(f)})
    save_new(a.out / 'weight-manifest.json', {'repo': a.repo, 'revision': a.revision, 'files': files,
                                            'trust_remote_code': False})
    print(json.dumps({'status': 'downloaded_and_hashed', 'repo':a.repo,'files':len(files)}))


if __name__ == '__main__':
    main()
