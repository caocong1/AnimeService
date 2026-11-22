import shutil
import subprocess

import pytest

from scripts.patch_artplayer_buffered import OLD, patch


def test_changing_buffer_ranges_use_one_snapshot(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required to execute the browser getter regression')
    path = tmp_path / 'artplayer.js'
    path.write_text('({' + OLD + '})', encoding='utf-8')
    exercise = """
const assert = require('node:assert/strict');
let reads = 0;
const e = {get buffered() {
    const length = ++reads === 2 ? 25 : 26;
    return {length, end(index) {
        if (index >= length) throw new RangeError('TimeRanges index out of bounds');
        return 100 + index;
    }};
}};
const getter = eval(require('node:fs').readFileSync(process.argv[1], 'utf8')).get;
"""
    broken = subprocess.run([node, '-e', exercise +
        "assert.throws(() => getter(), RangeError);", str(path)], capture_output=True, text=True)
    assert broken.returncode == 0, broken.stderr
    patch(path)
    fixed = path.read_bytes()
    patch(path)
    assert path.read_bytes() == fixed
    result = subprocess.run([node, '-e', exercise +
        "assert.equal(getter(), 125); assert.equal(reads, 1);", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unrecognized_vendor_is_not_modified(tmp_path):
    path = tmp_path / 'artplayer.js'
    path.write_text('changed upstream', encoding='utf-8')
    with pytest.raises(RuntimeError, match='Pinned Artplayer changed'):
        patch(path)
    assert path.read_text(encoding='utf-8') == 'changed upstream'
