"""Read-only comparison of this project's reviewed prompts and applied automations."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def prompt_sha(value):
    canonical = value.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n') + '\n'
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def preserved_sha(config):
    fields = {k: v for k, v in config.items() if k not in ('prompt', 'updated_at')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def check(manifest_path, automation_root):
    manifest_path, automation_root = Path(manifest_path), Path(automation_root)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    results = []
    for task in manifest['tasks']:
        identity = task['id']
        if task.get('managed_here') is not True:
            results.append({'id': identity, 'status': 'external_project', 'errors': []})
            continue
        errors = []
        row = {'id': identity, 'errors': errors}
        try:
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', identity):
                raise ValueError('invalid automation id')
            path = automation_root / identity / 'automation.toml'
            raw = path.read_bytes()
            if len(raw) > 512 * 1024:
                raise ValueError('configuration exceeds size limit')
            config = tomllib.loads(raw.decode('utf-8-sig'))
            template = (manifest_path.parent / task['template']).resolve()
            if not template.is_relative_to(manifest_path.parent.resolve()):
                raise ValueError('template outside prompt directory')
            template_sha = prompt_sha(template.read_text(encoding='utf-8-sig'))
            actual_sha = prompt_sha(config['prompt'])
            row.update(prompt_sha256_lf=actual_sha, template_sha256_lf=template_sha,
                       config_sha256=hashlib.sha256(raw).hexdigest())
            if not task.get('applied_config_sha256') or not task.get('preserved_fields_sha256'):
                errors.append('applied readback is not recorded')
            if template_sha != task.get('prompt_sha256_lf'):
                errors.append('template differs from reviewed manifest')
            if actual_sha != template_sha:
                errors.append('active prompt differs from template')
            if row['config_sha256'] != task.get('applied_config_sha256'):
                errors.append('active configuration differs from applied readback')
            if preserved_sha(config) != task.get('preserved_fields_sha256'):
                errors.append('preserved target, schedule or other configuration fields changed')
            for key in ('id', 'name', 'kind', 'rrule', 'status'):
                if config.get(key) != task.get(key):
                    errors.append(f'{key} differs from reviewed manifest')
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
            errors.append(f'cannot verify configuration: {type(error).__name__}')
        row['status'] = 'drift' if errors else 'verified'
        results.append(row)
    return {'schema': 1, 'ok': not any(r['errors'] for r in results), 'tasks': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', default=ROOT / 'docs/automation-prompts/manifest.json', type=Path)
    parser.add_argument('--automation-root', default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'automations', type=Path)
    args = parser.parse_args()
    result = check(args.manifest, args.automation_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
