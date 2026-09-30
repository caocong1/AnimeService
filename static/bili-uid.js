/* Bilibili's danmu sender hash is CRC32 of the decimal UID. CRC32 can be solved for its
   last four bytes, so fix every shorter prefix and keep the suffixes that are all digits. */
(function(root) {
  'use strict';
  const TABLE = new Uint32Array(256), TOP = new Uint8Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let j = 0; j < 8; j++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    TABLE[i] = c; TOP[c >>> 24] = i;
  }
  const step = (state, byte) => (state >>> 8) ^ TABLE[(state ^ byte) & 0xff];
  function crc32(text) {
    let state = 0xffffffff;
    for (let i = 0; i < text.length; i++) state = step(state, text.charCodeAt(i));
    return (state ^ 0xffffffff) >>> 0;
  }
  // Classic UIDs have at most 10 digits. Newer 16-digit UIDs have millions of collisions each.
  function uidCandidates(hash, maxDigits = 10) {
    if (typeof hash !== 'string' || !/^[0-9a-f]{1,8}$/i.test(hash)) return [];
    const target = parseInt(hash, 16) >>> 0, out = [];
    for (let uid = 1; uid < 1000; uid++) if (crc32(String(uid)) === target) out.push(String(uid));
    // The table lookups of the final four bytes follow from the final state alone.
    const k = [0, 0, 0, 0];
    let rest = (target ^ 0xffffffff) >>> 0;
    for (let i = 3; i >= 0; i--) {
      k[i] = TOP[(rest >>> (i * 8)) & 0xff];
      rest = (rest ^ (TABLE[k[i]] >>> ((3 - i) * 8))) >>> 0;
    }
    for (let digits = 4; digits <= maxDigits; digits++) {
      const size = digits - 4, from = size ? 10 ** (size - 1) : 0, to = size ? 10 ** size : 1;
      for (let n = from; n < to; n++) {
        const prefix = size ? String(n) : '';
        let state = 0xffffffff;
        for (let i = 0; i < prefix.length; i++) state = step(state, prefix.charCodeAt(i));
        let suffix = '';
        for (let i = 0; i < 4; i++) {
          const byte = (state ^ k[i]) & 0xff;
          if (byte < 48 || byte > 57 || (!prefix && !i && byte === 48)) { suffix = null; break; }
          suffix += String.fromCharCode(byte);
          state = step(state, byte);
        }
        if (suffix !== null) out.push(prefix + suffix);
      }
    }
    return out;
  }
  const api = {crc32, uidCandidates};
  if (typeof module !== 'undefined') module.exports = api;
  else root.BiliUid = api;
})(globalThis);
