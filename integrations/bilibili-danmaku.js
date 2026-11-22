// Bounded protobuf reader: HTTP 200 can contain a Bilibili error or HTML.
export function parseDanmakuBase64(base64) {
  const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  if (bytes.length > 16 * 1024 * 1024) throw Error('Bilibili segment too large');
  const strings = {6:'midHash',7:'content',10:'action',12:'idStr',14:'animation',16:'color_v2'};
  const numbers = {1:'id',2:'progress',3:'mode',4:'fontsize',5:'color',8:'ctime',9:'weight',11:'pool',13:'attr',15:'like_num',17:'dm_type_v2'};
  function message(data, element = false) {
    let pos = 0;
    const rows = [], row = {};
    function varint() {
      let n = 0n;
      for (let i=0;i<10;i++) {
        if (pos >= data.length) throw Error('Truncated Bilibili protobuf');
        const b=data[pos++];
        if (i===9 && b>1) throw Error('Invalid Bilibili varint');
        n |= BigInt(b & 127) << BigInt(i*7);
        if (b<128) return n;
      }
      throw Error('Invalid Bilibili varint');
    }
    while (pos<data.length) {
      const tag=Number(varint()), field=Math.floor(tag/8), wire=tag%8;
      if (field<1 || field>536870911) throw Error('Invalid Bilibili field');
      if (wire===0) {
        const value=varint();
        if (!element && field===2 && value!==0n) throw Error('Bilibili danmaku closed');
        if (element && numbers[field]) row[numbers[field]]=Number(value);
        if (element && field===1 && value>0n) row.idStr ??= String(value);
      } else if (wire===2 || wire===1 || wire===5) {
        const length=wire===2 ? Number(varint()) : wire===1 ? 8 : 4;
        if (!Number.isSafeInteger(length) || length>data.length-pos) throw Error('Truncated Bilibili field');
        const value=data.subarray(pos,pos+length);pos+=length;
        if (wire===2 && element && strings[field]) row[strings[field]]=new TextDecoder('utf-8',{fatal:true}).decode(value);
        if (wire===2 && !element && field===1) rows.push(message(value,true));
      } else throw Error('Invalid Bilibili wire type');
    }
    return element ? row : rows;
  }
  return message(bytes);
}

export async function parseBilibiliCommentXml(xml, cid) {
  if (typeof xml !== 'string' || xml.length > 4 * 1024 * 1024 || /<!DOCTYPE|<!ENTITY/i.test(xml)) {
    throw Error('Invalid Bilibili comment XML');
  }
  const { XMLParser, XMLValidator } = await import('fast-xml-parser');
  if (XMLValidator.validate(xml) !== true) throw Error('Malformed Bilibili comment XML');

  const parser = new XMLParser({
    ignoreAttributes: false,
    parseTagValue: false,
    trimValues: false,
    isArray: (_, path) => path === 'i.d',
  });
  const list = parser.parse(xml).i;
  if (list?.chatid !== cid) throw Error('Bilibili comment XML CID mismatch');

  return (list.d ?? []).map(row => {
    const fields = row['@_p']?.split(',');
    const content = row['#text'];
    if (!fields || fields.length < 8 || typeof content !== 'string' || !content.trim()) {
      throw Error('Invalid Bilibili comment XML row');
    }
    const [time, mode, fontsize, color, ctime, pool, midHash, idStr] = fields;
    if (!/^\d+(?:\.\d+)?$/.test(time) || ![mode, color].every(value => /^\d+$/.test(value))) {
      throw Error('Invalid Bilibili comment XML row');
    }
    return { progress: Math.round(Number(time) * 1000), mode: Number(mode), fontsize: Number(fontsize),
      color: Number(color), ctime: Number(ctime), pool: Number(pool), midHash, idStr, content };
  });
}

export function mergeBilibiliComments(segments, xml) {
  const comments = [];
  const seen = new Set();
  // Keep the segment copy of matching IDs for its likes and extended color.
  for (const rows of [segments, xml]) {
    for (const row of rows) {
      const id = row.idStr;
      if (id && seen.has(id)) continue;
      if (id) seen.add(id);
      comments.push({ ...row, preserveBilibiliTiming: true });
    }
  }
  return comments.sort((a, b) => (a.progress ?? 0) - (b.progress ?? 0));
}
