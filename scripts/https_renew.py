"""DNS-01 certificate maintenance with lego; every site value comes from data/https.json.

data/https.json is private and untracked:
{
  "domain": "home.example.tld",
  "email": "admin@example.tld",
  "dns_provider": "tencentcloud",
  "ddns_go_config": "~/.ddns_go_config.yaml",
  "resolver": "1.1.1.1:53",
  "propagation_timeout": 1800,
  "renew_days": 30
}
The provider credentials are read from ddns-go's own configuration, only in memory, and
never persisted or displayed; provider output can contain secret URLs, so only fixed status
categories are printed; provider output is never displayed.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'data/https.json'
# lego environment variables per DNS provider; extend deliberately, never guess.
PROVIDER_ENV = {'tencentcloud': ('TENCENTCLOUD_SECRET_ID', 'TENCENTCLOUD_SECRET_KEY')}


def bounded(config, key, default, lo, hi):
    value = config.get(key, default)
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError('invalid numeric setting')
    return value


def site(path=None):
    config = json.loads((path or CONFIG).read_text(encoding='utf-8'))
    for key in ('domain', 'email', 'dns_provider'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f'data/https.json needs {key}')
    if config['dns_provider'] not in PROVIDER_ENV:
        raise ValueError('unsupported dns_provider')
    # Domain is also a certificate filename; reject path components and controls.
    if (len(config['domain']) > 253 or not re.fullmatch(
            r'(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+'
            r'[a-zA-Z]{2,63}', config['domain'])):
        raise ValueError('invalid domain')
    for key in ('email', 'resolver', 'ddns_go_config'):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)):
            raise ValueError('invalid text setting')
    bounded(config, 'propagation_timeout', 1800, 30, 3600)
    bounded(config, 'renew_days', 30, 1, 90)
    return config


def credentials(config):
    ddns = yaml.safe_load(Path(config.get('ddns_go_config', '~/.ddns_go_config.yaml')).expanduser().read_text(encoding='utf-8'))
    providers = [x['dns'] for x in ddns['dnsconf'] if x.get('dns', {}).get('name', '').lower() == config['dns_provider']
                 and config['domain'] in x.get('ipv4', {}).get('domains', [])]
    if len(providers) != 1:
        raise ValueError('ambiguous or unavailable provider')
    values = providers[0]['id'], providers[0]['secret']
    if not all(isinstance(v, str) and v.strip() for v in values):
        raise ValueError('invalid credentials')
    return values


def main():
    config = site()
    account, secret = credentials(config)
    env = os.environ.copy()
    id_var, secret_var = PROVIDER_ENV[config['dns_provider']]
    env[id_var] = account
    env[secret_var] = secret
    env[f"{config['dns_provider'].upper()}_PROPAGATION_TIMEOUT"] = str(int(config.get('propagation_timeout', 1800)))
    dest = ROOT / 'data/tls'
    dest.mkdir(exist_ok=True)
    args = [str(ROOT / 'data/tools/lego.exe'), 'run', '--path', str(dest), '--accept-tos', '--email', config['email'],
            '--dns', config['dns_provider'], '--domains', config['domain']]
    if config.get('resolver'):
        args += ['--dns.resolvers', str(config['resolver'])]
    if (dest / f"certificates/{config['domain']}.crt").exists():
        args += ['--renew-days', str(int(config.get('renew_days', 30)))]
    try:
        result = subprocess.run(args, env=env, capture_output=True,
                                timeout=config.get('propagation_timeout', 1800) + 600,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        print('certificate_request_timeout', flush=True)
        return 1
    output = (result.stdout + result.stderr).decode(errors='replace')
    # Deliberately expose only fixed status categories, never provider output.
    category = 'ok' if result.returncode == 0 else ('dns_wait_failed' if 'propagation' in output else 'certificate_request_failed')
    print(category, flush=True)
    return 0 if result.returncode == 0 else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('certificate_setup_failed')
        sys.exit(1)
