const fs = require('fs');
const path = require('path');
const https = require('https');
const crypto = require('crypto');

const USER_ID = 'f056b790-1165-4405-bfe6-6ea65f94937e';
const CONN_DIR = path.join(process.env.USERPROFILE || 'C:/Users/CFZ', '.workbuddy/connectors', USER_ID);

function decryptAuth() {
  const st = JSON.parse(fs.readFileSync(path.join(CONN_DIR, 'connector-states.v3.json'), 'utf8'));
  const master = fs.readFileSync(path.join(CONN_DIR, '.master.key'));
  const salt = Buffer.from(st.encryption.salt, 'base64');
  const ikm = Buffer.concat([master, Buffer.from(USER_ID, 'utf8')]);
  const aesKey = Buffer.from(crypto.hkdfSync('sha256', ikm, salt,
    Buffer.from('workbuddy-oauth-credentials-v1', 'utf8'), 32));
  const env = st.headerOverrides.notion.Authorization;
  const d = crypto.createDecipheriv('aes-256-gcm', aesKey, Buffer.from(env.iv, 'base64'));
  d.setAAD(Buffer.from(`${USER_ID}|connector-states:headerOverrides:notion|Authorization`, 'utf8'));
  d.setAuthTag(Buffer.from(env.tag, 'base64'));
  return Buffer.concat([d.update(Buffer.from(env.ct, 'base64')), d.final()]).toString('utf8');
}

let sessionId = null;
let msgId = 0;

function rpc(method, params, id) {
  return new Promise((resolve) => {
    const body = JSON.stringify({ jsonrpc: '2.0', method, params, id });
    const req = https.request('https://mcp.notion.com/mcp', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'Authorization': decryptAuth(),
        ...(sessionId ? { 'Mcp-Session-Id': sessionId } : {}),
      },
    }, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => {
        const sid = res.headers['mcp-session-id'];
        if (sid) sessionId = sid;
        let obj = null;
        for (const line of data.split('\n')) {
          if (line.startsWith('data: ')) { try { obj = JSON.parse(line.slice(6)); } catch (e) {} }
        }
        if (!obj && data.trim().startsWith('{')) { try { obj = JSON.parse(data); } catch (e) {} }
        resolve({ status: res.statusCode, obj });
      });
    });
    req.on('error', (e) => resolve({ status: 0, obj: { error: String(e) } }));
    req.write(body);
    req.end();
  });
}

async function init() {
  await rpc('initialize', {
    protocolVersion: '2025-06-18', capabilities: {},
    clientInfo: { name: 'news-broadcast-tool', version: '1.0.0' },
  }, ++msgId);
  await rpc('notifications/initialized', {}, null);
}

async function callTool(name, args) {
  const r = await rpc('tools/call', { name, arguments: args }, ++msgId);
  const c = r.obj && r.obj.result && r.obj.result.content;
  if (!c) return r.obj;
  const txt = c.filter((x) => x.type === 'text').map((x) => x.text).join('\n');
  try { return JSON.parse(txt); } catch (e) { return txt; }
}

module.exports = { init, callTool, rpc };
