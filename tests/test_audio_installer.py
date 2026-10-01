import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from scripts import setup_audio_alignment as installer


def archive(tmp_path,monkeypatch,name='release/fpcalc.exe',symlink=False):
    path=tmp_path/'release.zip'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('release/','')
        entry=zipfile.ZipInfo(name)
        if symlink:entry.external_attr=0o120777<<16
        z.writestr(entry,b'verified binary')
        z.writestr('release/README.txt','official documentation')
    monkeypatch.setattr(installer,'FPCALC_SHA256',hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(installer,'_require_windows',lambda:None)
    return path


def existing(root):
    path=root/'tools/chromaprint/fpcalc.exe';path.parent.mkdir(parents=True)
    path.write_bytes(b'previous installation')
    return path


def test_official_directory_entry_flattening_and_notices(tmp_path,monkeypatch):
    release=archive(tmp_path,monkeypatch);root=tmp_path/'project'
    exe=installer.install(release,root)
    assert exe.read_bytes()==b'verified binary'
    assert (exe.parent/'README.txt').exists()
    assert (exe.parent/'chromaprint-LICENSE.md').is_file()
    assert (exe.parent/'chromaprint-LGPL-2.1.txt').is_file()
    assert (exe.parent/'SOURCE.txt').is_file()


@pytest.mark.parametrize('name,symlink',[('../fpcalc.exe',False),('C:/fpcalc.exe',False),('release/fpcalc.exe',True)])
def test_bad_members_preserve_existing_install(tmp_path,monkeypatch,name,symlink):
    release=archive(tmp_path,monkeypatch,name,symlink);root=tmp_path/'project';exe=existing(root)
    with pytest.raises(installer.InstallError):installer.install(release,root)
    assert exe.read_bytes()==b'previous installation'
    assert list(exe.parent.parent.iterdir())==[exe.parent]


def test_hash_mismatch_never_extracts_or_changes_old_tool(tmp_path,monkeypatch):
    release=archive(tmp_path,monkeypatch);root=tmp_path/'project';exe=existing(root)
    release.write_bytes(b'corrupt archive')
    with pytest.raises(installer.InstallError,match='SHA-256'):installer.install(release,root)
    assert exe.read_bytes()==b'previous installation'


@pytest.mark.parametrize('failure',['move_old','move_new'])
def test_rename_failure_keeps_or_restores_previous_install(tmp_path,monkeypatch,failure):
    release=archive(tmp_path,monkeypatch);root=tmp_path/'project';exe=existing(root)
    replace=Path.replace
    def fail(path,target):
        if failure=='move_old' and path==exe.parent or failure=='move_new' and path.name=='staging':raise OSError('simulated locked directory')
        return replace(path,target)
    monkeypatch.setattr(Path,'replace',fail)
    with pytest.raises(OSError):installer.install(release,root)
    assert exe.read_bytes()==b'previous installation'


def test_failed_rollback_keeps_recoverable_backup(tmp_path,monkeypatch):
    release=archive(tmp_path,monkeypatch);root=tmp_path/'project';exe=existing(root)
    replace=Path.replace
    def fail(path,target):
        if path.name=='staging' or path.name.startswith('chromaprint.previous-'):raise OSError('simulated OS failure')
        return replace(path,target)
    monkeypatch.setattr(Path,'replace',fail)
    with pytest.raises(OSError):installer.install(release,root)
    backup=list((root/'tools').glob('chromaprint.previous-*'))
    assert len(backup)==1;assert (backup[0]/'fpcalc.exe').read_bytes()==b'previous installation'


def test_download_cap(tmp_path,monkeypatch):
    monkeypatch.setattr(installer,'MAX_DOWNLOAD_BYTES',32)
    monkeypatch.setattr(installer.urllib.request,'urlopen',lambda *_args,**_kw:io.BytesIO(b'x'*64))
    with pytest.raises(installer.InstallError,match='limit'):installer._fetch_archive('https://example.test',tmp_path/'zip')
