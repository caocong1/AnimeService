"""Isolated control-plane publisher. No constructor/caller in the production app.

Consumes independently attested SHA-256, never mints source evidence. Explicit
trusted callbacks/keys/root only; no DB, network, inventory/progress writes or CLI.
Windows fixed-drive share locks protect the lease window, not permanent storage.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid
from types import MappingProxyType

from .playback_versions import fingerprint, file_identity


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def hex_value(value, length=64):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % length, value) is not None


def plain(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute plain path required')
    if any(':' in part for part in path.parts[1:]):
        raise ValueError('alternate data streams are unsupported')
    for part in [*reversed(path.parents), path]:
        value = part.lstat()
        if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
            raise ValueError('reparse paths are unsupported')
    return path


@contextmanager
def windows_file(path, *, exclusive=False):
    """Deny write/delete sharing; exclusive root lock never waits for other owners."""
    if os.name != 'nt':
        raise ValueError('Windows fixed-drive sealing is required')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    path = Path(path)
    if exclusive:
        plain(path.parent)
        if path.exists():
            plain(path)
    else:
        plain(path)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    if not re.fullmatch('[a-zA-Z]:', path.drive) or kernel.GetDriveTypeW(path.anchor) != 3:
        raise ValueError('local fixed drive required')
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW('\\\\?\\' + str(path), 0xC0000000 if exclusive else 0x80000000,
                                0 if exclusive else 1, None, 4 if exclusive else 3,
                                0x00200000 | 0x08000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ValueError('file unavailable or sharing conflict')
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_BINARY | (os.O_RDWR if exclusive else os.O_RDONLY))
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'r+b' if exclusive else 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('regular file required')
        yield stream


def load(stream):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate manifest field')
            result[key] = value
        return result
    raw = stream.read(32769)
    if len(raw) > 32768:
        raise ValueError('manifest too large')
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        if canonical(value) != raw:
            raise ValueError('manifest must be canonical')
        return value, digest(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('invalid manifest') from None


class PublicationLease:
    def __init__(self, issuer, stream, binding, version, path, manifest_digest, deadline, content_sha256):
        self._issuer, self._stream, self._binding = issuer, stream, binding
        self._deadline, self._active = deadline, True
        self.path, self.version_id, self.manifest_sha256 = path, version, manifest_digest
        self.media_key = digest(str(path).lower().encode())[:32]
        self.size = binding['size']
        # Registration consumes this immutable, validated snapshot, never mutable display attributes.
        self._proof = MappingProxyType({'media_key': self.media_key, 'path_key': str(path).lower(),
            'size': self.size, 'sha256': content_sha256, 'association_digest': binding['association_digest']})

    def assert_live(self):
        if not self._active or self._stream.closed or self._issuer.clock() > self._deadline:
            raise ValueError('publication lease is closed or expired')

    def check_binding(self, current):
        """Pure metadata CAS check; caller supplies facts read in its own DB transaction."""
        self.assert_live()
        if self._issuer.binding(current) != self._binding:
            raise ValueError('source authorization or mapping moved')

    def read(self, count=-1):
        self.assert_live()
        return self._stream.read(count)

    def proof(self):
        """Validated immutable facts, only for trusted same-transaction registration."""
        self.assert_live()
        return self._proof


class OrdinaryIssuer:
    def __init__(self, root, source_keys, publication_key, resolve_source, *, publication_key_id='owner-v1',
                 max_bytes=8*1024**3, time_limit=120, reserve_bytes=64*1024**2,
                 clock=time.monotonic, wall_clock=time.time):
        if (not isinstance(source_keys, dict) or not source_keys or any(not isinstance(k, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', k) or not isinstance(v, bytes)
                                   or len(v) < 32 for k, v in source_keys.items())
                or not isinstance(publication_key, bytes) or len(publication_key) < 32
                or not isinstance(publication_key_id, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', publication_key_id) or not callable(resolve_source)):
            raise ValueError('explicit trusted keys and resolver required')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 64*1024**3:
            raise ValueError('invalid publication byte budget')
        if isinstance(time_limit, bool) or not isinstance(time_limit, (int, float)) or not math.isfinite(time_limit) or not 0 < time_limit <= 600:
            raise ValueError('invalid publication time budget')
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise ValueError('invalid storage reserve')
        self.root = plain(Path(root))
        if not self.root.is_dir():
            raise ValueError('existing private publication directory required')
        self.keys, self.secret, self.key_id, self.resolve = dict(source_keys), publication_key, publication_key_id, resolve_source
        self.max_bytes, self.time_limit, self.reserve = max_bytes, time_limit, reserve_bytes
        self.clock, self.wall_clock = clock, wall_clock

    def source(self, envelope, at=None):
        if not isinstance(envelope, dict) or set(envelope) != {'schema', 'key_id', 'payload', 'mac'}:
            raise ValueError('source attestation required')
        key = self.keys.get(envelope['key_id']) if isinstance(envelope['key_id'], str) else None
        p = envelope['payload']
        if envelope['schema'] != 'ordinary-source/v1' or key is None or not hex_value(envelope['mac']):
            raise ValueError('untrusted source attestation')
        unsigned = {k: v for k, v in envelope.items() if k != 'mac'}
        if not hmac.compare_digest(envelope['mac'], hmac.digest(key, b'ordinary-source/v1\0' + canonical(unsigned), 'sha256').hex()):
            raise ValueError('source signature mismatch')
        fields = {'issuance_id', 'issued_at', 'expires_at', 'source_key', 'sha256', 'size',
                  'association_digest', 'input_signature', 'publisher'}
        if not isinstance(p, dict) or set(p) != fields:
            raise ValueError('invalid source fields')
        now = self.wall_clock() if at is None else at
        if (not hex_value(p['issuance_id'], 32) or not hex_value(p['source_key'], 32)
                or any(not hex_value(p[k]) for k in ('sha256', 'association_digest', 'input_signature'))
                or type(p['size']) is not int or p['size'] <= 0
                or type(p['issued_at']) is not int or type(p['expires_at']) is not int
                or not 0 <= p['issued_at'] <= now + 300 or not p['issued_at'] < p['expires_at'] <= p['issued_at'] + 86400
                or p['expires_at'] <= now or not isinstance(p['publisher'], str)
                or not re.fullmatch('[A-Za-z0-9_.-]{1,64}', p['publisher'])):
            raise ValueError('invalid or expired source attestation')
        return json.loads(canonical(p))

    @staticmethod
    def binding(row):
        if not isinstance(row, dict) or row.get('authorized') is not True:
            raise ValueError('source is outside authorized scope')
        if not isinstance(row.get('path'), str):
            raise ValueError('invalid authoritative source path')
        path = Path(row['path'])
        key = digest(str(path).lower().encode())[:32]
        if (not path.is_absolute() or '..' in path.parts or row.get('id') != key
                or type(row.get('size')) is not int or row['size'] <= 0
                or any(not hex_value(row.get(k)) for k in ('association_digest', 'input_signature'))):
            raise ValueError('invalid authoritative source binding')
        return {k: row[k] for k in ('id', 'path', 'size', 'association_digest', 'input_signature')} | {'authorized': True}

    def seal(self, body):
        return {'body': body, 'key_id': self.key_id,
                'mac': hmac.digest(self.secret, body['schema'].encode() + b'\0' + canonical(body), 'sha256').hex()}

    def unseal(self, envelope, schema):
        if (not isinstance(envelope, dict) or set(envelope) != {'body', 'key_id', 'mac'}
                or envelope['key_id'] != self.key_id or not isinstance(envelope['body'], dict)
                or envelope['body'].get('schema') != schema or not hex_value(envelope['mac'])):
            raise ValueError('untrusted publication manifest')
        if not hmac.compare_digest(envelope['mac'], self.seal(envelope['body'])['mac']):
            raise ValueError('publication signature mismatch')
        return envelope['body']

    def budget(self, deadline):
        if self.clock() > deadline:
            raise ValueError('publication time budget exceeded')

    def hash_stream(self, stream, size, deadline, sink=None):
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size or size > self.max_bytes:
            raise ValueError('publication size or byte budget mismatch')
        count, result = 0, hashlib.sha256()
        while True:
            self.budget(deadline)
            data = stream.read(min(1024**2, self.max_bytes-count+1))
            if not data:
                break
            count += len(data)
            if count > self.max_bytes:
                raise ValueError('publication byte budget exceeded')
            result.update(data)
            if sink is not None:
                sink.write(data)
        if count != size or fingerprint(before) != fingerprint(os.fstat(stream.fileno())):
            raise ValueError('publication changed while reading')
        self.budget(deadline)
        stream.seek(0)
        return result.hexdigest()

    @staticmethod
    def write(path, data):
        with path.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def pin_alias(self, path, body):
        temporary = path.parent / ('.pending-' + uuid.uuid4().hex)
        try:
            self.write(temporary, canonical(self.seal(body)))
            os.link(temporary, path)  # exclusive, no overwrite; crash cannot expose partial JSON
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def publish(self, evidence):
        evidence = json.loads(canonical(evidence))  # immutable request snapshot
        p = self.source(evidence)
        if p['size'] > self.max_bytes:
            raise ValueError('publication byte budget exceeded')
        initial = self.binding(self.resolve(p['source_key']))
        if any(initial[k] != p[k] for k in ('size', 'association_digest', 'input_signature')) or initial['id'] != p['source_key']:
            raise ValueError('source attestation does not bind current input')
        if Path(initial['path']).is_relative_to(self.root):
            raise ValueError('source cannot be inside publication storage')
        bound = {k: p[k] for k in ('sha256', 'size', 'association_digest')}
        version = digest(canonical({'schema': 'ordinary-content/v1', **bound}))
        folder = self.root/'objects'/version
        target = folder/'video.bin'
        media_key = digest(str(target).lower().encode())[:32]
        alias_path = self.root/'aliases'/(p['source_key']+'.json')
        deadline, lease = self.clock()+self.time_limit, None
        try:
            with ExitStack() as stack:
                stack.enter_context(windows_file(self.root/'.publisher.lock', exclusive=True))
                for directory in (self.root/'objects', self.root/'aliases'):
                    directory.mkdir(exist_ok=True)
                    plain(directory)
                alias = None
                if alias_path.exists():
                    alias = self.unseal(load(stack.enter_context(windows_file(alias_path)))[0], 'ordinary-alias/v1')
                    if alias.get('source_key') != p['source_key'] or alias.get('version_id') != version or alias.get('media_key') != media_key:
                        raise ValueError('ordinary alias rebind forbidden')
                source = stack.enter_context(windows_file(Path(initial['path'])))
                source_hashed = False
                if not folder.exists():
                    if shutil.disk_usage(self.root).free < p['size'] + self.reserve:
                        raise ValueError('publication storage budget exceeded')
                    staging = self.root/('.pending-'+uuid.uuid4().hex)
                    staging.mkdir()
                    try:
                        with (staging/'video.bin').open('xb') as output:
                            if self.hash_stream(source, p['size'], deadline, output) != p['sha256']:
                                raise ValueError('trusted content digest mismatch')
                            source_hashed = True
                            output.flush()
                            os.fsync(output.fileno())
                        body = {'schema': 'ordinary-publication/v1', 'version_id': version, 'media_key': media_key,
                                **bound, 'published_at': int(self.wall_clock()), 'source': evidence}
                        self.write(staging/'manifest.json', canonical(self.seal(body)))
                        self.budget(deadline)
                        os.rename(staging, folder)  # root lock serializes publishers, never replace an object
                    finally:
                        # Only this exclusive staging directory, two fixed files; never remove final objects.
                        if staging.exists():
                            plain(staging)
                            for name in ('video.bin', 'manifest.json'):
                                (staging/name).unlink(missing_ok=True)
                            staging.rmdir()
                plain(folder)
                manifest_stream = stack.enter_context(windows_file(folder/'manifest.json'))
                manifest, manifest_digest = load(manifest_stream)
                body = self.unseal(manifest, 'ordinary-publication/v1')
                if (body.get('version_id') != version or body.get('media_key') != media_key
                        or any(body.get(k) != v for k, v in bound.items())
                        or type(body.get('published_at')) is not int or body['published_at'] > self.wall_clock()+300):
                    raise ValueError('publication identity mismatch')
                historical = self.source(body.get('source'), at=body['published_at'])
                if any(historical[k] != v for k, v in bound.items()):
                    raise ValueError('publication source mismatch')
                stream = stack.enter_context(windows_file(target))
                if self.hash_stream(stream, p['size'], deadline) != p['sha256']:
                    raise ValueError('published content changed')
                if not source_hashed and self.hash_stream(source, p['size'], deadline) != p['sha256']:
                    raise ValueError('trusted content digest mismatch')
                if file_identity(os.fstat(source.fileno())) != file_identity(Path(initial['path']).stat()):
                    raise ValueError('source path changed')
                if self.binding(self.resolve(p['source_key'])) != initial:
                    raise ValueError('source authorization or mapping moved')
                self.source(evidence)
                wanted = {'schema': 'ordinary-alias/v1', 'source_key': p['source_key'], 'version_id': version,
                          'media_key': media_key, 'manifest_sha256': manifest_digest}
                if alias is None:
                    self.pin_alias(alias_path, wanted)
                    alias = self.unseal(load(stack.enter_context(windows_file(alias_path)))[0], 'ordinary-alias/v1')
                if alias != wanted:
                    raise ValueError('ordinary alias rebind forbidden')
                self.source(evidence)  # freshness must still hold after potentially long IO
                self.budget(deadline)
                lease = PublicationLease(self, stream, initial, version, target, manifest_digest, deadline, p['sha256'])
                yield lease
        except OSError:
            raise ValueError('publication storage or file unavailable') from None
        finally:
            if lease is not None:
                lease._active = False
