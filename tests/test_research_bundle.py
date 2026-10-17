import hashlib
import json
import stat
import zipfile

import pytest

from scripts.verify_research_bundle import SCHEMA, verify


def bundle(tmp_path, name='evidence/example.txt', content=b'synthetic evidence',
           digest=None, extra=None, missing=False, symlink=False):
    path = tmp_path / 'research.zip'
    manifest = {'schema': SCHEMA, 'files': [{
        'path': name, 'size': len(content),
        'sha256': digest or hashlib.sha256(content).hexdigest(),
    }]}
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('MANIFEST.json', json.dumps(manifest))
        if not missing:
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = ((stat.S_IFLNK if symlink else stat.S_IFREG) | 0o600) << 16
            archive.writestr(entry, content)
        if extra:
            archive.writestr(extra, b'unlisted')
    return path


def test_valid_bundle_is_verified_without_extracting(tmp_path):
    path = bundle(tmp_path)
    before = path.read_bytes()
    assert verify(path)['files'] == 1
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == before


def test_changed_payload_rejected(tmp_path):
    with pytest.raises(ValueError, match='SHA256'):
        verify(bundle(tmp_path, digest='0' * 64))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(KeyError):
        verify(bundle(tmp_path, missing=True))


def test_unlisted_file_rejected(tmp_path):
    with pytest.raises(ValueError, match='Unlisted'):
        verify(bundle(tmp_path, extra='unexpected.txt'))


def test_case_collision_rejected_for_windows_transfer(tmp_path):
    with pytest.raises(ValueError, match='Duplicate'):
        verify(bundle(tmp_path, extra='evidence/EXAMPLE.txt'))


@pytest.mark.parametrize('name', ['../outside.txt', '/absolute.txt', 'C:/file.txt',
                                   'evidence\\file.txt', 'evidence/NUL.txt', 'file.'])
def test_unsafe_member_paths_rejected(tmp_path, name):
    with pytest.raises(ValueError, match='archive path'):
        verify(bundle(tmp_path, name=name))


def test_symlink_rejected(tmp_path):
    with pytest.raises(ValueError, match='regular file'):
        verify(bundle(tmp_path, symlink=True))
