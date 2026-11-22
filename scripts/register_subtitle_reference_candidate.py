"""Plan an exact same-owner subtitle reference; --apply registers, never downloads."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.subtitle_delivery_queue import ControlStore
from anime.subtitle_reference_intake import register_reference_candidate


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
    output = args.output.resolve() if args.output else None
    if output and (not output.is_relative_to((db.path.parent / 'subtitle-studio').resolve()) or output.exists()):
        raise ValueError('Receipt must be a new private archive file')
    result = register_reference_candidate(db, args.request, apply=args.apply)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
