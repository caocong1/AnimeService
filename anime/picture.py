"""Read-only picture metadata. Unknown colour tags are never guessed or rewritten."""
import json
import math
import re
import threading


def number(value):
    try:
        if isinstance(value, str) and '/' in value:
            a, b = value.split('/')
            result = float(a) / float(b)
        else:
            result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def describe(stream):
    fields = ('codec_name', 'profile', 'pix_fmt', 'color_range', 'color_space',
              'color_transfer', 'color_primaries', 'chroma_location', 'field_order')
    result = {k: str(stream[k])[:80] if stream.get(k) not in (None, '', 'unknown', 'unspecified')
              else None for k in fields}
    for k in ('width', 'height'):
        result[k] = int(number(stream.get(k)) or 0)
    bits = number(stream.get('bits_per_raw_sample'))
    pixel = result['pix_fmt'] or ''
    if not bits:
        match = re.search(r'(?:p|gbrp|gray)(9|10|12|14|16)(?:le|be)?$', pixel)
        if match:
            bits = int(match[1])
        elif pixel in ('yuv420p', 'yuv422p', 'yuv444p', 'yuvj420p', 'yuvj422p',
                       'yuvj444p', 'nv12', 'rgb24', 'bgr24', 'gbrp', 'gray'):
            bits = 8
    result['bit_depth'] = int(bits) if bits else None
    result['fps'] = number(stream.get('avg_frame_rate')) or number(stream.get('r_frame_rate'))
    result['bit_rate'] = number(stream.get('bit_rate'))
    result['sample_aspect_ratio'] = str(stream.get('sample_aspect_ratio', ''))[:32]
    side = [str(x.get('side_data_type', ''))[:100] for x in stream.get('side_data_list', [])]
    result['hdr_metadata'] = [x for x in side if any(t in x.lower() for t in
        ('mastering display', 'content light', 'dovi', 'dolby', 'hdr'))]
    trc = result['color_transfer']
    result['dynamic_range'] = ('PQ' if trc == 'smpte2084' else 'HLG' if trc == 'arib-std-b67'
        else 'HDR metadata' if result['hdr_metadata'] else 'SDR' if trc in
        ('bt709', 'smpte170m', 'iec61966-2-1', 'gamma22', 'gamma28') else 'unknown')
    # Canvas/WebGL uploads are not a verified 10-bit/HDR output path. Only offer
    # the optional RGB scaler for explicitly tagged, progressive, square-pixel SDR.
    result['sdr_scaler_eligible'] = bool(result['bit_depth'] == 8 and
        result['dynamic_range'] == 'SDR' and result['color_primaries'] == 'bt709' and
        result['color_space'] == 'bt709' and result['color_range'] in ('tv', 'pc') and
        result['field_order'] == 'progressive' and
        result['sample_aspect_ratio'] in ('1:1', '1/1'))
    return result


class Picture:
    def __init__(self, tools):
        self.tools = tools
        self.lock = threading.Lock()
        self.cache = {}

    def probe(self, item):
        with self.lock:
            key = self.tools.fingerprint(item)
            if key in self.cache:
                return self.cache[key]
            raw = self.tools.run([self.tools.tool('ffprobe'), '-v', 'error',
                '-protocol_whitelist', 'file,pipe', '-select_streams', 'V:0',
                '-show_streams', '-of', 'json', item['path']], 25)
            streams = json.loads(raw).get('streams', [])
            if not streams:
                raise ValueError('未检测到视频流')
            result = {**describe(streams[0]), 'fingerprint': key,
                      'video_delivery': 'original_packets', 'video_transcoded': False}
            if key != self.tools.fingerprint(item):
                raise ValueError('文件已变化，请重新核查')
            if len(self.cache) >= 200:
                self.cache.clear()
            self.cache[key] = result
            return result
