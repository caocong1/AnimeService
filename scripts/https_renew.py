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
categories are printed and failure text is scrubbed before it is shown.
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


def site():
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    for key in ('domain', 'email', 'dns_provider'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f'data/https.json needs {key}')
    if config['dns_provider'] not in PROVIDER_ENV:
        raise ValueError('unsupported dns_provider')
    return config


def credentials(config):
    ddns = yaml.safe_load(Path(config.get('ddns_go_config', '~/.ddns_go_config.yaml')).expanduser().read_text(encoding='utf-8'))
    provider = next(x['dns'] for x in ddns['dnsconf'] if x.get('dns', {}).get('name', '').lower() == config['dns_provider']
                    and config['domain'] in x.get('ipv4', {}).get('domains', []))
    return provider['id'], provider['secret']


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
    result = subprocess.run(args, env=env, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    output = (result.stdout + result.stderr).decode(errors='replace')
    # Deliberately expose only fixed status categories, never provider output.
    category = 'ok' if result.returncode == 0 else ('dns_wait_failed' if 'propagation' in output else 'certificate_request_failed')
    print(category, flush=True)
    if result.returncode:
        clean = output.replace(secret, '[redacted]').replace(account, '[redacted]')
        clean = re.sub(r'https?://[^\s"<>]+', '[url omitted]', clean)
        clean = re.sub(r'(?i)(key|secret|token|authorization|cookie)[=:]\s*\S+', r'\1=[redacted]', clean)
        print(clean[-1800:])
    return result.returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('certificate_setup_failed:', type(e).__name__)
        sys.exit(1)
