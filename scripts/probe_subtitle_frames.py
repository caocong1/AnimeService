"""Extract evidence frames from a bound episode on the paired Mac."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.mac_subtitle_media_worker import options
from scripts.probe_subtitle_decode import bound_media
from scripts.subtitle_output_lock import output_lock
from scripts.subtitle_remote_media import preparation, unpack_frames
from scripts.subtitle_remote_render import sha


def verified_existing(output, spec, identity):
    receipt = output / "frames.json"
    record = json.loads(receipt.read_text(encoding="utf-8"))
    saved = dict(record["request"])
    source_sha = saved.pop("source_sha256")
    images = record["images"]
    if (saved != spec or any(record.get(k) != v for k, v in identity.items())
            or not re.fullmatch("[0-9a-f]{64}", source_sha) or not 1 <= len(images) <= 1500):
        raise ValueError("Existing frame receipt does not match this job and request")
    for name, expected in images.items():
        if not re.fullmatch(r"frame-[0-9]{4}\.(?:jpg|png)", name) or sha(output / name) != expected:
            raise ValueError("Existing verified frame changed")
    return receipt


def collect(db_path, sid, episode, owner, expected_input, spec, output):
    output = output.resolve()
    if not any(output.is_relative_to(db_path.parent / name) for name in ("subtitle-studio", "diagnostics")):
        raise ValueError("Choose a private subtitle or diagnostics directory")
    options(spec)
    deployment = db_path.parent / "subtitle-render-worker.json"
    host = json.loads(deployment.read_text(encoding="utf-8-sig"))["host"]
    args = (db_path, sid, episode, owner, expected_input)
    media = bound_media(*args)[0]
    identity = dict(show_id=sid, episode=episode, fingerprint=media[1],
                    target=list(media), owner=owner, input=expected_input)

    def recheck():
        if bound_media(*args)[0] != media:
            raise ValueError("Episode input changed during sampling")

    with output_lock(output.parent / ("." + output.name + ".lock"), "Mac subtitle frames", timeout=0):
        if output.exists():
            receipt = verified_existing(output, spec, identity)
            recheck()
            return receipt
        source = Path(media[0])
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        scratch = Path(tempfile.mkdtemp(dir=output.parent))
        try:
            with preparation(host, source, digest, db_path.parent, spec, recheck) as (archive, record):
                record.update(identity)
                unpack_frames(archive, scratch, record)
            recheck()
            scratch.rename(output)
        finally:
            if scratch.exists():
                shutil.rmtree(scratch)
    return output / "frames.json"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("show_id", type=int)
    p.add_argument("episode", type=int)
    p.add_argument("--owner")
    p.add_argument("--expected-input")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--timestamps", nargs="+", type=float)
    group.add_argument("--range", nargs=3, type=float, metavar=("START", "END", "EVERY"))
    p.add_argument("--width", type=int, default=1280, help="0 to retain native resolution")
    p.add_argument("--format", choices=("png", "jpg"), default="jpg")
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    spec = dict(mode="frames", width=args.width, format=args.format)
    if args.timestamps:
        spec["timestamps"] = args.timestamps
    else:
        spec["start"], spec["end"], spec["every"] = args.range
    print(collect(ROOT / "data/anime.db", args.show_id, args.episode, args.owner, args.expected_input,
                  spec, args.output))


if __name__ == "__main__":
    main()
