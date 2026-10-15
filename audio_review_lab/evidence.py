from __future__ import annotations

import hashlib
import json
import math
import wave
from array import array
from fractions import Fraction
from pathlib import Path


def peak_rss_bytes():
    import os
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    class PMC(ctypes.Structure):
        _fields_ = [('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)] + [
            (n,ctypes.c_size_t) for n in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage',
            'QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    kernel=ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    fn=ctypes.windll.psapi.GetProcessMemoryInfo
    fn.argtypes=[wintypes.HANDLE,ctypes.POINTER(PMC),wintypes.DWORD]
    fn.restype=wintypes.BOOL
    counters=PMC();counters.cb=ctypes.sizeof(counters)
    if not fn(kernel.GetCurrentProcess(),ctypes.byref(counters),counters.cb):
        return None
    return counters.PeakWorkingSetSize


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save_new(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def audit_wav(path: Path) -> dict:
    with wave.open(str(path), 'rb') as w:
        if (w.getnchannels(), w.getframerate(), w.getsampwidth(), w.getcomptype()) != (1, 16000, 2, 'NONE'):
            raise ValueError('Only mono/16kHz/PCM16 is admitted; never resample implicitly')
        count = w.getnframes()
        if not 0 < count <= 30 * 16000:
            raise ValueError('Window must be within (0,30] seconds')
        data = w.readframes(count)
        if len(data) != count * 2:
            raise ValueError('Truncated WAV data')
    samples = array('h', data)
    return {'wav_sha256': sha256(path), 'sample_count': count, 'sample_rate': 16000,
            'duration_seconds': count / 16000, 'channels': 1, 'sample_width': 2,
            'peak': max(abs(x) for x in samples) / 32768,
            'rms': math.sqrt(sum(x * x for x in samples) / count) / 32768,
            'dc': sum(samples) / count / 32768,
            'clipped_fraction': sum(abs(x) >= 32767 for x in samples) / count}


def map_sample(sample: int, sections: list[dict]) -> Fraction:
    """Map only proven continuous sections; gaps and negative indices are errors."""
    for s in sections:
        if s['sample_start'] <= sample < s['sample_end']:
            return Fraction(s['media_start']) + Fraction(sample - s['sample_start'], s['rate'])
    raise ValueError('Sample is outside verified continuous mapping')


def validate_observation(record: dict) -> None:
    if record.get('status') != 'proposal_only':
        raise ValueError('Models cannot produce final decisions')
    for key in ('human_listened', 'current_codex_listened', 'subtitle_acceptance'):
        if record.get(key) is not False:
            raise ValueError('Machine observations must not claim listening acceptance')
    if record.get('observed_media_span') is not None:
        raise ValueError('An experiment request is not an observed speech boundary')
    if record.get('timing_is_estimated') is not True:
        raise ValueError('ASR timings remain estimates')
    if record.get('scope') not in (['lexical_proposal'], ['separation_derivative']):
        raise ValueError('Recognition cannot claim speaker or timing acceptance')
