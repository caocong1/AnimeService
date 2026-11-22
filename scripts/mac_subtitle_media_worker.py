"""Content-verified source staging and serialized subtitle preparation on the paired Mac."""
import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

BASE = Path.home() / ".animeservice-worker"
GIB = 1024 ** 3
CACHE_LIMIT = 48 * GIB


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextlib.contextmanager
def locked(path):
    import fcntl
    with path.open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield lock


def job_root(name):
    if not re.fullmatch(r"(?:probe|render)-[a-f0-9]{32}", name):
        raise ValueError("Invalid job directory")
    root = BASE / name
    if root.resolve() != root:
        raise ValueError("Unexpected job path")
    return root


def make_room(cache, target, size, upload):
    others = sorted((p for p in cache.iterdir() if re.fullmatch("[a-f0-9]{64}", p.name) and p != target),
                    key=lambda p: p.stat().st_mtime)
    total = size + sum(p.stat().st_size for p in others)
    reserve = (size if upload else 0) + 2 * GIB
    for old in others:
        if total <= CACHE_LIMIT and shutil.disk_usage(cache).free >= reserve:
            break
        total -= old.stat().st_size
        old.unlink()  # Active jobs hold their own hard links.
    if shutil.disk_usage(cache).free < reserve:
        raise OSError("Insufficient Mac staging space")


def stage(root, expected, size, upload):
    if not re.fullmatch("[a-f0-9]{64}", expected) or size <= 0:
        raise ValueError("Invalid source identity")
    cache = BASE / "source-cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    target = cache / expected
    dest = root / "source.media"
    # Pin this inode before validation so eviction cannot change the bytes we return.
    # A cache hit does not need to wait for another episode's upload.
    if not upload and not dest.exists() and not target.is_symlink():
        try:
            os.link(target, dest)
        except FileNotFoundError:
            pass
        else:
            if not dest.is_symlink() and dest.stat().st_size == size and digest(dest) == expected:
                os.utime(dest)
                print("linked")
                return
            dest.unlink()
    with locked(cache / ".lock"):
        for partial in cache.glob("incoming-*.part"):
            partial.unlink()  # The cache lock also covers the entire upload.
        if target.is_symlink():
            raise ValueError("Unexpected link in Mac source cache")
        if target.exists() and (target.stat().st_size != size or digest(target) != expected):
            target.unlink()  # Drop only the corrupt cache link; jobs keep their own files.
        if not target.exists():
            # Older, already-running renderers uploaded their own source. Adopt only identical bytes.
            for candidate in BASE.glob("render-*/source.media"):
                try:
                    if candidate.is_symlink() or candidate.stat().st_size != size or digest(candidate) != expected:
                        continue
                    os.link(candidate, target)
                    break
                except FileNotFoundError:
                    continue
        if not target.exists() and not upload:
            print("missing")
            return
        make_room(cache, target, size, not target.exists())
        if not target.exists():
            with tempfile.NamedTemporaryFile(dir=cache, prefix="incoming-", suffix=".part", delete=False) as handle:
                temp = Path(handle.name)
            try:
                copied = 0
                hashed = hashlib.sha256()
                with temp.open("wb") as output:
                    while chunk := sys.stdin.buffer.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > size:
                            raise ValueError("Source length changed")
                        output.write(chunk)
                        hashed.update(chunk)
                if copied != size or hashed.hexdigest() != expected:
                    raise ValueError("Incomplete or changed upload")
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
        if target.stat().st_size != size:
            raise ValueError("Cached source length changed")
        dest = root / "source.media"
        if not dest.exists():
            os.link(target, dest)
        elif not dest.samefile(target):
            raise FileExistsError(dest)
        os.utime(target)
        if size > CACHE_LIMIT:
            target.unlink()
        print("linked")


def options(spec):
    mode = spec["mode"]
    if mode == "decode":
        return mode, [], 0, "jpg"
    if mode != "frames":
        raise ValueError("Unknown preparation operation")
    width = spec["width"]
    fmt = spec["format"]
    if type(width) != int or not 0 <= width <= 7680 or fmt not in ("jpg", "png"):
        raise ValueError("Invalid image format or width")
    points = spec.get("timestamps", [])
    if points:
        if len(points) > 500 or any(not math.isfinite(x) or not 0 <= x < 86400 for x in points):
            raise ValueError("Invalid frame time")
    else:
        start, end, every = (float(spec[k]) for k in ("start", "end", "every"))
        if not (0 <= start < end <= 86400 and math.isfinite(every) and every > 0 and
                1 <= (end - start) / every <= 1500):
            raise ValueError("Invalid frame range")
    return mode, points, width, fmt


def process(root):
    import imageio_ffmpeg
    spec = json.loads((root / "request.json").read_bytes())
    mode, points, width, fmt = options(spec)
    if not (root / "source.media").is_file():
        raise ValueError("Missing staged source")
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-n", "-loglevel", "error", "-xerror",
           "-threads", "2", "-filter_threads", "2", "-hwaccel", "videotoolbox"]
    with locked(root / ".lock"), locked(BASE / "prepare.lock") as slot:
        def run(args):
            subprocess.run(cmd + args, cwd=root, check=True, pass_fds=(slot.fileno(),), timeout=7200)
        if mode == "decode":
            run(["-stats_period", "10", "-progress", "decode.progress", "-i", "source.media",
                 "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn", "-f", "null", "-"])
            return
        scales = [f"scale={width}:-2"] if width else []
        suffix = ["-map", "0:v:0", "-an", "-sn", "-dn", "-threads", "2"]
        if fmt == "jpg":
            suffix += ["-q:v", "3"]
        if points:
            if scales:
                suffix += ["-vf", scales[0]]
            for index, seconds in enumerate(points, 1):
                path = f"frame-{index:04d}.{fmt}"
                run(["-ss", str(seconds), "-i", "source.media", *suffix, "-frames:v", "1", path])
                if not (root / path).is_file():
                    raise ValueError("Requested sample is beyond the video")
        else:
            start, end, every = (float(spec[k]) for k in ("start", "end", "every"))
            filters = [f"fps=1/{every}:start_time=0", *scales]
            run(["-ss", str(start), "-t", str(end-start), "-i", "source.media", *suffix,
                 "-vf", ",".join(filters), f"frame-%04d.{fmt}"])
        images = sorted(root.glob("frame-*." + fmt))
        if not images or (not points and abs(len(images) - (end-start)/every) > 1):
            raise ValueError("Incomplete frame series")
        checks = {p.name: digest(p) for p in images}
        (root / "frames.json").write_text(json.dumps(dict(request=spec, images=checks)))
        with tarfile.open(root / "frames.tar", "x") as archive:
            for path in [root / "frames.json", *images]:
                archive.add(path, arcname=path.name)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("job")
    p.add_argument("action", choices=("prepare", "clean", "attach", "upload", "process"))
    p.add_argument("digest", nargs="?")
    p.add_argument("size", nargs="?", type=int)
    args = p.parse_args()
    os.umask(0o077)
    root = job_root(args.job)
    if args.action in ("prepare", "clean", "process") and not args.job.startswith("probe-"):
        raise ValueError("Preparation work directory required")
    if args.action == "prepare":
        root.mkdir(mode=0o700)
    elif not root.is_dir():
        raise ValueError("Missing job directory")
    elif args.action == "clean":
        with locked(root / ".lock"):
            shutil.rmtree(root)
    elif args.action == "process":
        process(root)
    else:
        stage(root, args.digest, args.size, args.action == "upload")


if __name__ == "__main__":
    main()
