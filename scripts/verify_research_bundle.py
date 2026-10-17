"""Read-only integrity check for a private research ZIP; never extracts or runs it."""
import argparse
import hashlib
import json
import re
import stat
import zipfile

SCHEMA = 'anime-subtitle-research-bundle/v1'
MAX_MEMBER = 32 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024


def safe_name(name):
    if not isinstance(name, str) or not name or any(c in name for c in '\\:<>"|?*\0'):
        raise ValueError('Unsafe archive path')
    for part in name.split('/'):
        if not part or part in ('.', '..') or part.endswith((' ', '.')):
            raise ValueError('Unsafe archive path')
        if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I):
            raise ValueError('Reserved archive path')
    return name


def verify(path):
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 10000 or sum(x.file_size for x in members) > MAX_TOTAL:
            raise ValueError('Bundle exceeds bounded research size')
        names = set()
        for item in members:
            name = safe_name(item.filename)
            if name.casefold() in names:
                raise ValueError('Duplicate archive path')
            names.add(name.casefold())
            if item.is_dir() or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('Only regular file members are supported')
            if item.file_size > MAX_MEMBER or item.flag_bits & 1:
                raise ValueError('Oversized or encrypted member')
        manifest = json.loads(archive.read('MANIFEST.json'))
        if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA:
            raise ValueError('Unknown manifest schema')
        records = manifest.get('files')
        if not isinstance(records, list):
            raise ValueError('Missing file records')
        expected = {'MANIFEST.json'}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('Invalid file record')
            name = safe_name(record.get('path'))
            if name in expected:
                raise ValueError('Duplicate manifest path')
            expected.add(name)
            checksum, size = record.get('sha256'), record.get('size')
            if (not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum)
                    or type(size) is not int or size < 0):
                raise ValueError('Invalid digest or size')
            item = archive.getinfo(name)
            if item.file_size != size:
                raise ValueError('Size mismatch: ' + name)
            with archive.open(item) as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != checksum:
                raise ValueError('SHA256 mismatch: ' + name)
        if expected != {item.filename for item in members}:
            raise ValueError('Unlisted or missing archive members')
        return {'verified': True, 'files': len(records),
                'created_at': manifest.get('created_at'),
                'git_revision': manifest.get('git_revision'),
                'note': 'Integrity only; no extraction, execution or quality acceptance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('zip_path')
    args = parser.parse_args()
    try:
        result = verify(args.zip_path)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
        parser.exit(1, f'Bundle verification failed: {exc}\n')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
