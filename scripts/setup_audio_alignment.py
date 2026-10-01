"""Install the Chromaprint fpcalc binary used for audio alignment.

Windows-only helper: downloads the pinned official fpcalc 1.6.1 release
(or consumes a caller-supplied archive), verifies its SHA-256, extracts it
safely, and atomically swaps it into ``<root>/tools/chromaprint`` with
rollback so a failed install preserves the previous installation.

Stdlib only; performs no downloader, credential, media-library, or
database operations.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

FPCALC_URL = (
    "https://github.com/acoustid/chromaprint/releases/download/"
    "v1.6.1/chromaprint-fpcalc-1.6.1-windows-x86_64.zip"
)
FPCALC_SHA256 = (
    "735d6182b38e9f364b84ce6f4ccd682c75e2851de89735711d6b762d12b92a4e"
)
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB
HTTP_TIMEOUT = 30  # seconds
_CHUNK = 64 * 1024


class InstallError(RuntimeError):
    """Raised when the fpcalc install cannot be completed safely."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise InstallError(
            "setup_audio_alignment installs fpcalc on Windows only; "
            f"current platform is {sys.platform!r}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_archive(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` bounded by size and timeout."""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
            total = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise InstallError(
                            f"fpcalc archive exceeds {MAX_DOWNLOAD_BYTES} byte limit"
                        )
                    fh.write(chunk)
    except InstallError:
        raise
    except Exception as exc:  # network errors surface as InstallError
        raise InstallError(f"failed to download fpcalc archive: {exc}") from exc


def _member_target(name: str) -> PurePosixPath:
    """Validate a zip member name and return its safe relative path."""
    if not name or name.startswith("/") or "\\" in name:
        raise InstallError(f"unsafe archive member name: {name!r}")
    rel = PurePosixPath(name)
    if PureWindowsPath(name).drive or any(part in ("..", "") for part in rel.parts):
        raise InstallError(f"unsafe archive member name: {name!r}")
    if any(":" in part for part in rel.parts):
        raise InstallError(f"unsafe archive member name: {name!r}")
    return rel


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode stored in high bits of external_attr; S_IFLNK = 0o120000.
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _extract_archive(archive: Path, staging: Path) -> None:
    """Extract a verified archive into ``staging``, flattening the top dir."""
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise InstallError(f"fpcalc archive is not a valid zip: {exc}") from exc

    with zf:
        infos = [i for i in zf.infolist() if i.filename and i.filename != "/"]
        if not infos:
            raise InstallError("fpcalc archive is empty")
        for info in infos:
            if _is_symlink(info):
                raise InstallError(
                    f"archive member is a symlink: {info.filename!r}"
                )
            _member_target(info.filename)

        members = [PurePosixPath(i.filename) for i in infos]
        files = [m for i,m in zip(infos,members) if not i.is_dir()]
        top = files[0].parts[0] if files else ''
        strip = 1 if files and all(m.parts[0] == top and len(m.parts) > 1 for m in files) else 0

        for info, rel in zip(infos, members):
            if info.is_dir() or info.filename.endswith("/"):
                continue
            target = staging.joinpath(*rel.parts[strip:])
            if not target.resolve().is_relative_to(staging.resolve()):
                raise InstallError(
                    f"archive member escapes staging dir: {info.filename!r}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)

    if not (staging / "fpcalc.exe").is_file():
        raise InstallError("archive does not contain fpcalc.exe")


def _swap_into_place(staging: Path, tool_dir: Path) -> None:
    """Atomically replace ``tool_dir`` with ``staging``, rolling back on failure."""
    backup = tool_dir.with_name(tool_dir.name + '.previous-' + secrets.token_hex(6))
    if tool_dir.exists() and tool_dir.resolve().parent != tool_dir.parent.resolve():
        raise InstallError('existing tool directory resolves outside the intended tools directory')

    moved_aside = False
    try:
        if tool_dir.exists():
            tool_dir.replace(backup)
            moved_aside = True
        staging.replace(tool_dir)
    except Exception:
        if moved_aside and tool_dir.exists():
            _remove_dir(tool_dir,tool_dir.parent)
        if moved_aside and backup.exists():
            backup.replace(tool_dir)
        raise

    if moved_aside:
        _remove_dir(backup,tool_dir.parent)


def _remove_dir(path: Path, parent: Path) -> None:
    if path.resolve().parent != parent.resolve():
        raise InstallError('cleanup path resolves outside the intended tools directory')
    shutil.rmtree(path,ignore_errors=True)


def install(archive: Path | None = None, root: Path | None = None) -> Path:
    """Install fpcalc and return the installed ``fpcalc.exe`` path.

    ``archive``: optional local zip; when supplied the download is skipped
    but the pinned SHA-256 is still enforced.
    ``root``: project root; defaults to this repo's root. The binary lands
    at ``<root>/tools/chromaprint/fpcalc.exe`` alongside licenses/docs.
    """
    _require_windows()

    project_root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    tool_dir = project_root / "tools" / "chromaprint"
    tool_dir.parent.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.mkdtemp(prefix="fpcalc-", dir=tool_dir.parent))
    staging = tmp_dir / "staging"
    staging.mkdir()
    try:
        if archive is not None:
            archive_path = Path(archive)
            if not archive_path.is_file():
                raise InstallError(f"archive not found: {archive_path}")
        else:
            archive_path = tmp_dir / "fpcalc.zip"
            _fetch_archive(FPCALC_URL, archive_path)

        digest = _sha256_file(archive_path)
        if digest.lower() != FPCALC_SHA256.lower():
            raise InstallError(
                f"fpcalc archive SHA-256 mismatch: got {digest}, "
                f"expected {FPCALC_SHA256}"
            )

        _extract_archive(archive_path, staging)
        # The official Windows ZIP contains only the executable. Keep upstream
        # notices with the local installation; these reviewed copies ship in source.
        notices = Path(__file__).resolve().parents[1] / 'integrations'
        for name in ('chromaprint-LICENSE.md','chromaprint-LGPL-2.1.txt'):
            shutil.copyfile(notices / name, staging / name)
        (staging/'SOURCE.txt').write_text('Chromaprint 1.6.1 official binary: '+FPCALC_URL+'\n'
            'Source and build instructions: https://github.com/acoustid/chromaprint/tree/v1.6.1\n',encoding='utf-8')
        _swap_into_place(staging, tool_dir)
    finally:
        _remove_dir(tmp_dir,tool_dir.parent)

    return tool_dir / "fpcalc.exe"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive",
        nargs="?",
        type=Path,
        default=None,
        help="optional local fpcalc zip; download is skipped but hash is enforced",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="project root (default: repository root)",
    )
    args = parser.parse_args(argv)
    try:
        exe = install(archive=args.archive, root=args.root)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
