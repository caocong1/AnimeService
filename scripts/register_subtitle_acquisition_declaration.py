"""Register exact publisher soft-track evidence; default is a read-only plan."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.subtitle_delivery_queue import ControlStore
from anime.subtitle_acquisition_declarations import register_acquisition_declaration
from anime.subtitle_chinese import digest


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'data/anime.db')
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    db = ControlStore(args.db.resolve())
    root = (db.path.parent / 'subtitle-studio').resolve()
    output = args.output.resolve() if args.output else root / 'acquisition-declaration-receipts' / (str(time.time_ns()) + '.json')
    if not output.is_relative_to(root) or output.exists():
        raise ValueError('Declaration receipt must be a new private archive path')
    result = register_acquisition_declaration(db, args.request, apply=args.apply)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({k: result[k] for k in ('candidate_id', 'show_id', 'episode', 'id', 'infohash',
        'applied', 'idempotent', 'metadata_changed', 'media_mode', 'declaration_only',
        'clean_frames_verified', 'download_submitted', 'ready', 'accepted', 'finished')}
        | {'receipt': str(output), 'sha256': digest(output.read_bytes())}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
