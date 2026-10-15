const net = require('node:net');
const crypto = require('node:crypto');
const [threadId, turnId] = process.argv.slice(2);
const allowed = new Set(['01a10c1a-eea3-7070-bd80-5f8cee6426a1', '01a125c3-0885-7191-9acd-0789c73f6ebb']);
if (!allowed.has(threadId) || !/^[0-9a-f-]{36}$/.test(turnId || '')) throw Error('outside dispatcher scope');
const method = 'thread-follower-interrupt-turn', versionText = '4', targetClientId = undefined;
const params = {conversationId:threadId, mode:'system', expectedTurnId:turnId};
const pending = new Map();
let clientId = 'uninitialized', buffer = Buffer.alloc(0);
const socket = net.connect('\\\\.\\pipe\\codex-ipc');
const deadline = setTimeout(() => { console.error('bounded IPC timeout'); socket.destroy(); process.exitCode = 2; }, 20000);
function send(m) {
  const b = Buffer.from(JSON.stringify(m)); const h = Buffer.alloc(4); h.writeUInt32LE(b.length);
  socket.write(Buffer.concat([h, b]));
}
function request(method, params, version, targetClientId) {
  const requestId = crypto.randomUUID();
  return new Promise((resolve, reject) => {
    pending.set(requestId, {resolve, reject});
    send({type:'request', requestId, sourceClientId:clientId, method, params, version, targetClientId, timeoutMs:15000});
  });
}
socket.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  while (buffer.length >= 4) {
    const n = buffer.readUInt32LE(0);
    if (n > 268435456) throw Error('frame exceeds app limit');
    if (buffer.length < n + 4) break;
    const m = JSON.parse(buffer.subarray(4, n+4)); buffer = buffer.subarray(n+4);
    if (m.type === 'client-discovery-request') send({type:'client-discovery-response',requestId:m.requestId,response:{canHandle:false}});
    if (m.type === 'response' && pending.has(m.requestId)) {
      const p = pending.get(m.requestId); pending.delete(m.requestId); p.resolve(m);
    }
  }
});
socket.once('error', e => { console.error(e.message); clearTimeout(deadline); process.exitCode = 1; });
socket.once('connect', async () => {
  try {
    const init = await request('initialize', {clientType:'codex-diagnostics'}, 0);
    if (init.resultType !== 'success') throw Error(JSON.stringify(init));
    clientId = init.result.clientId;
    const result = await request(method, params, Number(versionText), targetClientId);
    console.log(JSON.stringify(result));
    if (result.resultType !== 'success') process.exitCode = 1;
  } catch (e) { console.error(e.message); process.exitCode = 1; }
  finally { clearTimeout(deadline); socket.end(); }
});
