const net = require('node:net');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
// Same dispatcher list as codex_dispatch_guard.py; ANIMESERVICE_DISPATCH_CONFIG points both at another copy.
function loadConfig(filename = process.env.ANIMESERVICE_DISPATCH_CONFIG || path.join(__dirname, 'codex_dispatch.json')) {
  const config = JSON.parse(fs.readFileSync(filename, 'utf8').replace(/^\uFEFF/, ''));
  if (!config?.targets || typeof config.targets !== 'object' || Array.isArray(config.targets)
      || !Object.keys(config.targets).length || Object.entries(config.targets).some(([id,automation]) => !id.trim() || typeof automation !== 'string' || !automation.trim()))
    throw Error('invalid dispatcher targets');
  return config;
}
function validateScope(config, threadId, turnId) {
  if (!Object.hasOwn(config.targets, threadId) || !/^[0-9a-f-]{36}$/.test(turnId || '')) throw Error('outside dispatcher scope');
  if (config.exact_turns_only && config.recovery_turns?.[threadId] !== turnId) throw Error('outside exact turn scope');
}
module.exports = {loadConfig, validateScope};
function main() {
const [threadId, turnId] = process.argv.slice(2);
validateScope(loadConfig(), threadId, turnId);
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
}
if (require.main === module) main();
