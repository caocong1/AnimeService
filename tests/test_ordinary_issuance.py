"""Real Windows sharing + isolated synthetic evidence; never imports production DB."""
import copy
import hashlib
import hmac
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from anime import ordinary_issuance as mod

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows share-mode contract')


@pytest.fixture
def env(tmp_path):
    root = tmp_path/'private'; root.mkdir()
    src = tmp_path/'source.mkv'; src.write_bytes(b'A'*8192)
    row = {'authorized': True, 'path': str(src), 'id': mod.digest(str(src).lower().encode())[:32],
           'size': src.stat().st_size, 'association_digest': 'a'*64, 'input_signature': 'b'*64}
    rows = {row['id']: row}
    now = [1800000000]
    secret = b's'*32
    issuer = mod.OrdinaryIssuer(root, {'test': secret}, b'p'*32, rows.__getitem__,
                                wall_clock=lambda: now[0], reserve_bytes=0)
    def evidence(r=None, **changes):
        r = r or row
        payload = {'issuance_id': '1'*32, 'issued_at': now[0]-1, 'expires_at': now[0]+60,
                   'source_key': r['id'], 'sha256': mod.digest(Path(r['path']).read_bytes()),
                   'size': r['size'], 'association_digest': r['association_digest'],
                   'input_signature': r['input_signature'], 'publisher': 'fixture'}
        payload.update(changes)
        value = {'schema': 'ordinary-source/v1', 'key_id': 'test', 'payload': payload}
        value['mac'] = hmac.digest(secret, b'ordinary-source/v1\0'+mod.canonical(value), 'sha256').hex()
        return value
    return SimpleNamespace(root=root, src=src, row=row, rows=rows, now=now, issuer=issuer, evidence=evidence)


def test_publish_retry_canonical_identity_and_closed_lease(env):
    original = env.src.read_bytes()
    with env.issuer.publish(env.evidence()) as lease:
        assert lease.read() == original
        identity = (lease.version_id, lease.media_key, lease.manifest_sha256, lease.path)
        lease.check_binding(env.row)
        manifest_bytes = (lease.path.parent/'manifest.json').read_bytes()
    with pytest.raises(ValueError, match='closed'):
        lease.read()
    with env.issuer.publish(env.evidence(issuance_id='2'*32)) as retry:
        assert (retry.version_id, retry.media_key, retry.manifest_sha256, retry.path) == identity
        assert (retry.path.parent/'manifest.json').read_bytes() == manifest_bytes
    assert env.src.read_bytes() == original
    assert not list(env.root.rglob('*.db'))
    assert not list(env.root.glob('.pending-*'))


@pytest.mark.parametrize('operation', ['write', 'unlink', 'rename'])
def test_real_share_lock_denies_source_and_publication_mutations(env, operation):
    with env.issuer.publish(env.evidence()) as lease:
        for path in (env.src, lease.path, lease.path.parent/'manifest.json',
                     env.root/'aliases'/(env.row['id']+'.json')):
            with pytest.raises(OSError):
                if operation == 'write':
                    with path.open('r+b') as output:
                        output.write(b'X')
                elif operation == 'unlink':
                    path.unlink()
                else:
                    path.rename(path.with_suffix('.moved'))
        with pytest.raises(ValueError, match='sharing conflict'):
            with env.issuer.publish(env.evidence()):
                pytest.fail('second publisher must fail without waiting')


def test_existing_writer_refused_without_closing_it(env):
    with env.src.open('r+b') as writer:
        with pytest.raises(ValueError, match='sharing conflict'):
            with env.issuer.publish(env.evidence()):
                pytest.fail('must not seal a writable open input')
        assert not writer.closed
    assert not list((env.root/'objects').iterdir())


@pytest.mark.parametrize('mutation', ['mac', 'key_id', 'payload', 'unsigned', 'expired', 'future', 'size', 'mapping', 'input'])
def test_untrusted_or_unbound_source_refused_before_publication(env, mutation):
    evidence = env.evidence()
    if mutation == 'mac': evidence['mac'] = '0'*64
    elif mutation == 'key_id': evidence['key_id'] = []
    elif mutation == 'payload': evidence['payload']['sha256'] = '0'*64
    elif mutation == 'unsigned': evidence = evidence['payload']
    elif mutation == 'expired': evidence = env.evidence(expires_at=env.now[0])
    elif mutation == 'future': evidence = env.evidence(issued_at=env.now[0]+400, expires_at=env.now[0]+500)
    elif mutation == 'size': evidence = env.evidence(size=8193)
    elif mutation == 'mapping': evidence = env.evidence(association_digest='c'*64)
    elif mutation == 'input': evidence = env.evidence(input_signature='d'*64)
    with pytest.raises(ValueError):
        with env.issuer.publish(evidence): pytest.fail('invalid evidence accepted')
    assert not list(env.root.glob('objects/*'))


def test_digest_mismatch_cleans_staging_preserves_source(env):
    with pytest.raises(ValueError, match='digest mismatch'):
        with env.issuer.publish(env.evidence(sha256='0'*64)): pass
    assert env.src.read_bytes() == b'A'*8192
    assert not list(env.root.glob('.pending-*'))
    assert not list((env.root/'objects').iterdir())
    assert not list((env.root/'aliases').iterdir())


def test_old_source_alias_never_rebinds_and_new_source_has_independent_identity(env):
    with env.issuer.publish(env.evidence()) as first:
        old_key, old_path = first.media_key, first.path
    env.src.write_bytes(b'B'*8192)
    with pytest.raises(ValueError, match='rebind forbidden'):
        with env.issuer.publish(env.evidence()): pass
    assert old_path.read_bytes() == b'A'*8192
    new = copy.deepcopy(env.row)
    new_path = env.src.with_name('new-version.mkv'); new_path.write_bytes(b'B'*8192)
    new.update(path=str(new_path), id=mod.digest(str(new_path).lower().encode())[:32])
    env.rows[new['id']] = new
    with env.issuer.publish(env.evidence(new)) as second:
        assert second.media_key != old_key
        assert second.path != old_path


def test_same_content_alias_uses_one_canonical_identity(env):
    with env.issuer.publish(env.evidence()) as lease:
        first = lease.media_key, lease.version_id
    other = copy.deepcopy(env.row)
    path = env.src.with_name('other-alias.mkv'); path.write_bytes(env.src.read_bytes())
    other.update(path=str(path), id=mod.digest(str(path).lower().encode())[:32])
    env.rows[other['id']] = other
    with env.issuer.publish(env.evidence(other)) as lease:
        assert (lease.media_key, lease.version_id) == first
    assert len(list((env.root/'objects').iterdir())) == 1
    assert len(list((env.root/'aliases').iterdir())) == 2


def test_reuse_rehashes_both_original_and_publication(env):
    evidence = env.evidence()
    with env.issuer.publish(evidence) as lease: path = lease.path
    env.src.write_bytes(b'B'*8192)
    with pytest.raises(ValueError, match='digest mismatch'):
        with env.issuer.publish(evidence): pass
    env.src.write_bytes(b'A'*8192)
    path.write_bytes(b'B'*8192)
    with pytest.raises(ValueError, match='published content changed'):
        with env.issuer.publish(evidence): pass


@pytest.mark.parametrize('mutation', ['mapping', 'scope', 'input', 'deleted'])
def test_binding_change_during_work_never_pins_alias(env, mutation):
    original = env.issuer.hash_stream
    def move(*args, **kwargs):
        result = original(*args, **kwargs)
        if mutation == 'mapping': env.row['association_digest'] = 'f'*64
        elif mutation == 'scope': env.row['authorized'] = False
        elif mutation == 'input': env.row['input_signature'] = 'f'*64
        elif mutation == 'deleted': env.row['path'] = str(env.src.with_name('missing'))
        return result
    env.issuer.hash_stream = move
    with pytest.raises(ValueError):
        with env.issuer.publish(env.evidence()): pass
    assert not list((env.root/'aliases').iterdir())


def test_transaction_binding_is_explicit_pure_and_lease_scoped(env):
    with env.issuer.publish(env.evidence()) as lease:
        moved = dict(env.row, association_digest='f'*64)
        with pytest.raises(ValueError, match='mapping moved'): lease.check_binding(moved)
        lease.check_binding(env.row)
    with pytest.raises(ValueError, match='closed'): lease.check_binding(env.row)


@pytest.mark.parametrize('budget', ['bytes', 'time', 'disk', 'freshness'])
def test_budget_failures_are_visible_and_leave_no_alias(env, monkeypatch, budget):
    if budget == 'bytes': env.issuer.max_bytes = 8191
    elif budget == 'time':
        ticks = iter([0, 121, 122, 123, 124])
        env.issuer.clock = lambda: next(ticks)
    elif budget == 'disk': monkeypatch.setattr(mod.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1))
    else:
        original = env.issuer.hash_stream
        def expire(*args, **kwargs):
            result = original(*args, **kwargs); env.now[0] += 61; return result
        env.issuer.hash_stream = expire
    with pytest.raises(ValueError):
        with env.issuer.publish(env.evidence()): pass
    assert not list(env.root.glob('aliases/*.json'))
    assert not list(env.root.glob('.pending-*'))


def test_crash_after_publication_before_alias_is_recoverable(env, monkeypatch):
    real = env.issuer.pin_alias
    monkeypatch.setattr(env.issuer, 'pin_alias', lambda *_: (_ for _ in ()).throw(RuntimeError('synthetic crash')))
    with pytest.raises(RuntimeError, match='synthetic crash'):
        with env.issuer.publish(env.evidence()): pass
    assert len(list((env.root/'objects').iterdir())) == 1
    assert not list((env.root/'aliases').iterdir())
    monkeypatch.setattr(env.issuer, 'pin_alias', real)
    with env.issuer.publish(env.evidence()) as lease:
        assert lease.read() == env.src.read_bytes()
    assert len(list((env.root/'objects').iterdir())) == 1


@pytest.mark.parametrize('which', ['manifest', 'alias'])
def test_tampered_sealed_metadata_rejected_without_repair(env, which):
    with env.issuer.publish(env.evidence()) as lease:
        path = lease.path.parent/'manifest.json' if which == 'manifest' else env.root/'aliases'/(env.row['id']+'.json')
    raw = path.read_bytes(); path.write_bytes(raw.replace(b'"mac":"', b'"mac":"0', 1))
    tampered = path.read_bytes()
    with pytest.raises(ValueError):
        with env.issuer.publish(env.evidence()): pass
    assert path.read_bytes() == tampered


def test_published_manifest_authentic_after_original_attestation_expires(env):
    with env.issuer.publish(env.evidence()) as first: identity = first.media_key
    env.now[0] += 600
    with env.issuer.publish(env.evidence()) as retry: assert retry.media_key == identity


def test_missing_source_never_uses_cached_publication(env):
    evidence = env.evidence()
    with env.issuer.publish(evidence): pass
    env.src.unlink()
    with pytest.raises(ValueError, match='unavailable'):
        with env.issuer.publish(evidence): pass


def test_junction_source_rejected(env):
    import _winapi
    junction = env.src.parent/'junction'
    actual = env.src.parent/'actual'; actual.mkdir()
    (actual/'video.mkv').write_bytes(b'A'*8192)
    _winapi.CreateJunction(str(actual), str(junction))
    try:
        with pytest.raises(ValueError, match='reparse'):
            with mod.windows_file(junction/'video.mkv'): pass
    finally:
        junction.rmdir()


def test_lease_deadline_is_enforced_for_db_handoff(env):
    tick = [0]
    env.issuer.clock = lambda: tick[0]
    with env.issuer.publish(env.evidence()) as lease:
        tick[0] = 121
        with pytest.raises(ValueError, match='expired'): lease.check_binding(env.row)
