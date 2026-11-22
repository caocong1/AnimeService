"""Restore only the enrolled loopback audio runtime. No installs, pulls or business writes.

Executable/model identities and the original environment are pinned in a private
configuration. A healthy listener is left alone; an occupied/unidentified port
is never killed. The dispatcher records failures in its recovery journal.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from anime.subtitle_recovery import read_json


def validate(config):
    exe = Path(config['executable'])
    models = Path(config['models'])
    if not exe.is_absolute() or not exe.is_file() or not models.is_absolute():
        raise ValueError('registered local runtime is missing')
    if hashlib.sha256(exe.read_bytes()).hexdigest() != config['executable_sha256']:
        raise ValueError('runtime changed; review required before restart')
    manifest = models / 'manifests/registry.ollama.ai/library/gemma4/e2b'
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != config['model_digest']:
        raise ValueError('registered model changed; no pull or substitution allowed')
    parsed = json.loads(raw)
    for ref in [parsed['config'], *parsed['layers']]:
        digest = ref['digest']
        if len(digest) != 71 or not digest.startswith('sha256:') or any(c not in '0123456789abcdef' for c in digest[7:]):
            raise ValueError('invalid registered model digest')
        blob = models / 'blobs' / digest.replace(':', '-')
        if not blob.is_file() or blob.stat().st_size != ref['size']:
            raise ValueError('installed model asset missing; automatic download forbidden')
    return exe, models


def request(route, body=None):
    # A redirect cannot change the endpoint because the operation is never
    # allowed to leave this exact loopback origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError('audio health redirected')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request('http://127.0.0.1:11436' + route, data=data,
                                 headers={'Content-Type': 'application/json'})
    with opener.open(req, timeout=3) as response:
        raw = response.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('oversized audio health response')
    return json.loads(raw)


def inspect(config):
    version = request('/api/version')['version']
    model = next((m for m in request('/api/tags')['models'] if m['name'] == 'gemma4:e2b'), None)
    if not model or model['digest'] != config['model_digest']:
        raise ValueError('listener model does not match enrollment')
    capabilities = request('/api/show', {'model': 'gemma4:e2b'}).get('capabilities', [])
    if 'audio' not in capabilities:
        raise ValueError('listener has no audio encoder')
    return {'version': version, 'model_digest': model['digest'], 'capabilities': capabilities}


def repair(config, output, apply=False):
    output = Path(output).resolve()
    if not output.is_relative_to((ROOT / 'data/subtitle-studio').resolve()):
        raise ValueError('private subtitle archive output required')
    output.mkdir(parents=True, exist_ok=True)
    try:
        return {'status': 'healthy', 'started': False, 'capability': inspect(config)}
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    # An unconfirmed prior launch is not permission for another process.
    intent_path = output / 'start.json'
    prior_confirmed_dead = False
    if intent_path.is_file():
        previous = read_json(intent_path)
        try:
            previous_process = psutil.Process(previous['pid'])
            if previous_process.create_time() <= previous['started_at'] + 2:
                raise RuntimeError('previous audio startup still exists; reconcile instead of relaunching')
            prior_confirmed_dead = True  # PID reused by a later process
        except psutil.NoSuchProcess:
            prior_confirmed_dead = True
    # A refusing TCP connection is different from an unhealthy occupied service.
    # Never terminate or replace somebody else's listener.
    with socket.socket() as check:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        check.bind(('127.0.0.1', 11436))
    exe, models = validate(config)
    if not apply:
        return {'status': 'start_required', 'started': False, 'validated': True}
    launch_path = output / 'launch-intent.json'
    if launch_path.exists() and prior_confirmed_dead:
        # Keep the old receipt. Remove only our exact intent after proof that
        # its recorded process is gone and the fixed port is unoccupied.
        launch_path.replace(output / ('retired-intent-' + str(time.time_ns()) + '.json'))
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('OLLAMA_') and k.upper() not in
           ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')}
    env.update({'OLLAMA_HOST': '127.0.0.1:11436', 'OLLAMA_MODELS': str(models),
                'OLLAMA_NO_CLOUD': '1', 'OLLAMA_NOPRUNE': '1', 'OLLAMA_VULKAN': '1',
                'OLLAMA_CONTEXT_LENGTH': '2048', 'OLLAMA_KEEP_ALIVE': '2m',
                'OLLAMA_NUM_PARALLEL': '1', 'OLLAMA_MAX_LOADED_MODELS': '1',
                'OLLAMA_ORIGINS': 'http://127.0.0.1', 'NO_PROXY': '127.0.0.1,localhost'})
    # Persist before spawn. A crash between spawn and its receipt stays blocked
    # for explicit reconciliation, rather than launching a duplicate runtime.
    with launch_path.open('x', encoding='utf-8') as stream:
        json.dump({'requested_at': time.time()}, stream)
    with (output / 'runtime.stdout.log').open('ab') as stdout, (output / 'runtime.stderr.log').open('ab') as stderr:
        process = subprocess.Popen([str(exe), 'serve'], env=env, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    intent = {'pid': process.pid, 'started_at': time.time(), 'port': 11436,
              'no_cloud': True, 'no_download': True, 'original_environment_restored': True}
    intent_path.write_text(json.dumps(intent, indent=2), encoding='utf-8')
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('enrolled audio runtime exited; inspect saved stderr')
        try:
            result = inspect(config)
            # This is service capability only. No utterance/listening claim.
            return {'status': 'restored', 'started': True, **intent, 'capability': result}
        except (urllib.error.URLError, OSError):
            time.sleep(.3)
    raise RuntimeError('audio startup unconfirmed; preserve process/intent and reconcile before retry')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'data/subtitle-studio/recovery/audio-runtime.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = repair(read_json(args.config), args.output, args.apply)
    (args.output / 'health-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
