"""Mac-side encoder. The kernel lock follows ffmpeg across exec and disconnects."""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    base = Path.home() / ".animeservice-worker"
    name, action = sys.argv[1:3]
    if not re.fullmatch(r"render-[0-9a-f]{32}", name):
        raise ValueError("Invalid render directory")
    root = base / name
    os.umask(0o077)
    if action == "prepare":
        if len(sys.argv) == 4 and shutil.disk_usage(base).free < 2 * int(sys.argv[3]) + 2**30:
            raise OSError("Insufficient free space for input and rendered movie on the Mac")
        root.mkdir(mode=0o700)
        (root / "fonts").mkdir()
        return
    if root.resolve() != root or not root.is_dir():
        raise ValueError("Invalid render directory")
    os.chdir(root)
    with contextlib.ExitStack() as stack:
        lock = stack.enter_context((root / ".lock").open("a+b"))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action == "clean":
            shutil.rmtree(root)
            return
        spec = json.loads(Path("render.json").read_bytes())
        if action == "verify":
            for path, expected in spec["inputs"].items():
                if sha(Path(path)) != expected:
                    raise ValueError(f"Changed input {path}")
        elif action == "encode":
            slot = stack.enter_context((base / "render.lock").open("a+b"))
            fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for stream in (lock, slot):
                os.set_inheritable(stream.fileno(), True)
            os.execv(ffmpeg, [ffmpeg, "-nostats", *spec["args"]])
        elif action == "check":
            movie = Path("output.mp4")
            subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(movie),
                            "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], check=True)
            print(json.dumps({"sha256": sha(movie), "bytes": movie.stat().st_size, "decode": True}))
        else:
            raise ValueError("Unknown render action")


if __name__ == "__main__":
    main()
