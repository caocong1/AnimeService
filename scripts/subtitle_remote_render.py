"""Stage frozen inputs on the paired Mac, return a hash-verified decoded MP4."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def connection(host):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Invalid SSH host alias")
    return ["-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oConnectTimeout=6",
            "-oServerAliveInterval=10", "-oServerAliveCountMax=3", "-oForwardAgent=no"]


def worker_command():
    version = sha(Path(__file__).with_name("mac_render_worker.py"))
    return f"~/.animeservice-worker/render-env/bin/python ~/.animeservice-worker/render-{version}.py"


def available(host):
    # This runs before any job is sent. A dropped live job is never replayed.
    worker = worker_command().split()
    from scripts.subtitle_remote_media import worker as media_worker
    media_script = media_worker().split()[1]
    return subprocess.run(["ssh", *connection(host), host, f"test -x {worker[0]} && test -r {worker[1]} && test -r {media_script}"],
                          capture_output=True, timeout=25).returncode == 0


def render(host, args, root, source_sha, encode, lease, outcome):
    opts = connection(host)
    job = "render-" + uuid.uuid4().hex
    prefix = worker_command() + " " + job + " "
    ssh = ["ssh", *opts, host]
    scp = ["scp", "-q", *opts]
    remote = host + ":.animeservice-worker/" + job + "/"
    movie = Path(args[-1])
    source = Path(args[args.index("-i") + 1])
    remote_args = list(args[1:])
    remote_args[remote_args.index("-i") + 1] = "source.media"
    remote_args[remote_args.index("-progress") + 1] = "pipe:1"
    remote_args[-1] = "output.mp4"
    inputs = [(source, "source.media", source_sha),
              (root / "bilingual.ass", "bilingual.ass", sha(root / "bilingual.ass"))]
    for idx, path in enumerate(sorted((root / "fonts").iterdir())):
        inputs.append((path, f"fonts/{idx}{path.suffix.lower()}", sha(path)))
    receipt = root / "remote-render.json"
    def record(stage, **facts):
        temp = receipt.with_suffix(".tmp")
        temp.write_text(json.dumps({"host": host, "directory": job, "stage": stage,
                                    "source_sha256": source_sha, **facts}), encoding="utf-8")
        temp.replace(receipt)

    record("staging")
    subprocess.run(ssh + [prefix + f"prepare {source.stat().st_size}"], check=True, timeout=30)
    returned = False
    try:
        from scripts.subtitle_remote_media import stage_source
        staging = stage_source(host, job, source, source_sha, Path(__file__).resolve().parents[1] / "data")
        outcome["source_staging"] = staging
        for path, name, _ in inputs[1:]:
            subprocess.run(scp + [str(path), remote + name], check=True, timeout=3600)
        spec = {"args": remote_args, "inputs": {name: digest for _, name, digest in inputs}}
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "render.json"
            manifest.write_text(json.dumps(spec), encoding="utf-8")
            subprocess.run(scp + [str(manifest), remote + "render.json"], check=True, timeout=30)
        subprocess.run(ssh + [prefix + "verify"], check=True, timeout=600)
        record("encoding", source_staging=staging)
        outcome.update(worker=host, remote_job=job)
        # A PTY sends SIGHUP to the foreground encoder on SSH disconnect.
        encode(["ssh", "-n", "-tt", *opts, host, "exec " + prefix + "encode"],
               root, lease, outcome)
        result = subprocess.run(ssh + [prefix + "check"], check=True, stdout=subprocess.PIPE, timeout=1200)
        checked = json.loads(result.stdout)
        partial = movie.with_name(movie.name + ".partial")
        record("returning_verified_movie", **checked)
        for attempt in range(3):
            try:
                subprocess.run(scp + [remote + "output.mp4", str(partial)], check=True, timeout=3600)
                if (checked["decode"] is not True or partial.stat().st_size != checked["bytes"]
                        or sha(partial) != checked["sha256"]):
                    raise ValueError("Returned movie differs from the decoded Mac output")
                break
            except (subprocess.SubprocessError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        if movie.exists():
            raise FileExistsError(movie)
        os.rename(partial, movie)
        record("returned", source_staging=staging, **checked)
        returned = True
        return checked
    finally:
        # Retain an encoded movie if verification/return failed; the receipt identifies it.
        if returned or not outcome.get("spawned"):
            try:
                subprocess.run(ssh + [prefix + "clean"], check=True, timeout=45)
            except (OSError, subprocess.SubprocessError) as error:
                print(f"Mac workspace cleanup pending ({job}): {error}", flush=True)
