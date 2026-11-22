"""Verified media transport shared by preparation and the established Mac renderer."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid

from scripts.subtitle_remote_render import connection, sha
from scripts.subtitle_output_lock import output_lock


def worker():
    version = sha(Path(__file__).with_name("mac_subtitle_media_worker.py"))
    return f"~/.animeservice-worker/render-env/bin/python ~/.animeservice-worker/media-{version}.py"


def run(cmd, *, timeout, guard=None, stdin=None, stdout=None):
    with tempfile.TemporaryFile() as log, subprocess.Popen(
            cmd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else log, stderr=log) as proc:
        deadline = time.monotonic() + timeout
        try:
            while True:
                if guard:
                    guard()
                try:
                    proc.wait(timeout=min(10, max(.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for {cmd[0]}")
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        log.seek(0)
        message = log.read()[-8192:].decode(errors="replace").strip()
        if proc.returncode:
            raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {message}")
        if guard:
            guard()
        return message


def stage_source(host, job, source, expected, data_dir, guard=None):
    if not re.fullmatch("[0-9a-f]{64}", expected) or not re.fullmatch("(?:render|probe)-[0-9a-f]{32}", job):
        raise ValueError("Invalid subtitle staging identity")
    size = source.stat().st_size
    prefix = ["ssh", *connection(host), host]
    request = worker() + " " + job
    identity = f" {expected} {size}"
    with output_lock(data_dir / "subtitle-studio" / "source-upload-locks" / (expected + ".lock"),
                     "Mac source transfer", timeout=3600):
        reply = run(prefix + [request + " attach" + identity], timeout=600, guard=guard)
        if reply == "linked":
            return "reused"
        if reply != "missing":
            raise ValueError("Unexpected Mac source-cache response")
        with source.open("rb") as payload:
            reply = run(prefix + [request + " upload" + identity], timeout=3600, stdin=payload, guard=guard)
        if reply != "linked":
            raise ValueError("Mac did not verify the staged bytes")
        return "uploaded"


@contextmanager
def preparation(host, source, expected, data_dir, spec, guard):
    job = "probe-" + uuid.uuid4().hex
    opts = connection(host)
    prefix = worker() + " " + job + " "
    ssh = ["ssh", *opts, host]
    scp = ["scp", "-q", *opts]
    remote = host + ":.animeservice-worker/" + job + "/"
    guard()
    run(ssh + [prefix + "prepare"], timeout=30)
    try:
        route = stage_source(host, job, source, expected, data_dir, guard)
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "request.json"
            spec = {**spec, "source_sha256": expected}
            path.write_text(json.dumps(spec), encoding="utf-8")
            run(scp + [str(path), remote + path.name], timeout=60, guard=guard)
            run(["ssh", "-n", "-tt", *opts, host, "exec " + prefix + "process"], timeout=10800, guard=guard)
            name = "decode.progress" if spec["mode"] == "decode" else "frames.tar"
            result = Path(work) / name
            run(scp + [remote + name, str(result)], timeout=900, guard=guard)
            yield result, dict(host=host, source_staging=route, request=spec, remote_job=job)
    finally:
        try:
            run(ssh + [prefix + "clean"], timeout=45)
        except (OSError, RuntimeError, TimeoutError) as exc:
            print(f"Mac preparation cleanup pending for {job}: {exc}", flush=True)


def unpack_frames(archive, result_dir, record):
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        if (len(members) > 1501 or any(not m.isfile() or not re.fullmatch(
                r"frames\.json|frame-[0-9]{4}\.(?:jpg|png)", m.name) for m in members)
                or len(set(m.name for m in members)) != len(members)):
            raise ValueError("Unexpected Mac image archive")
        entry = tar.getmember("frames.json")
        if entry.size > 512 * 1024:
            raise ValueError("Oversized image manifest")
        manifest = json.load(tar.extractfile(entry))
        images = manifest["images"]
        if (manifest["request"] != record["request"] or not images or
                set(images) != {m.name for m in members if m.name != "frames.json"}):
            raise ValueError("Returned images do not match this request")
        for name, digest in images.items():
            with tar.extractfile(name) as src, (result_dir / name).open("xb") as dest:
                shutil.copyfileobj(src, dest)
            if sha(result_dir / name) != digest:
                raise ValueError(f"Returned image changed: {name}")
        (result_dir / "frames.json").write_text(json.dumps({**record, "images": images}, indent=2), encoding="utf-8")
