#!/usr/bin/env node
/**
 * 新闻联播 → Notion 批量归档（通过 Notion 官方 MCP server）
 *
 * 用法：
 *   node notion_mcp_sync.js <docs_dir> [parent_page_id] [--limit N] [--skip-existing]
 *
 * 说明：
 *   - 认证复用 WorkBuddy 已授权的 Notion 连接器（OAuth），凭据从本地
 *     connector-states.v3.json 解出（算法同 WorkBuddy CredentialFieldCipher）
 *   - 内容用 Notion Markdown 直接提交，由 MCP server 转成原生 blocks
 *   - 状态文件 notion_mcp_state.json 记录已同步日期，重跑只补新增
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const crypto = require('crypto');

const USER_ID = 'f056b790-1165-4405-bfe6-6ea65f94937e';
const CONN_DIR = path.join(process.env.USERPROFILE || 'C:/Users/CFZ', '.workbuddy/connectors', USER_ID);
const STATES = path.join(CONN_DIR, 'connector-states.v3.json');
const MASTER = path.join(CONN_DIR, '.master.key');
const MCP_URL = 'https://mcp.notion.com/mcp';

// ---------- 解密 Notion OAuth 头 ----------
function decryptStates() {
  const st = JSON.parse(fs.readFileSync(STATES, 'utf8'));
  const master = fs.readFileSync(MASTER);
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

// ---------- MCP client ----------
let sessionId = null;
let msgId = 0;
let authHeader = null;

function rpc(method, params, id) {
  return new Promise((resolve) => {
    const body = JSON.stringify({ jsonrpc: '2.0', method, params, id });
    const req = https.request(MCP_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'Authorization': authHeader,
        ...(sessionId ? { 'Mcp-Session-Id': sessionId } : {}),
      },
    }, (res) => {
      let data = '';
      res.on('data', c => data += c);
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
    req.on('error', e => resolve({ status: 0, obj: { error: String(e) } }));
    req.write(body); req.end();
  });
}

async function init() {
  authHeader = decryptStates();
  await rpc('initialize', {
    protocolVersion: '2025-06-18', capabilities: {},
    clientInfo: { name: 'news-broadcast-sync', version: '1.0.0' },
  }, ++msgId);
  await rpc('notifications/initialized', {}, null);
}

async function callTool(name, args) {
  const r = await rpc('tools/call', { name, arguments: args }, ++msgId);
  const c = r.obj && r.obj.result && r.obj.result.content;
  if (!c) return r.obj;
  const txt = c.filter(x => x.type === 'text').map(x => x.text).join('\n');
  try { return JSON.parse(txt); } catch (e) { return txt; }
}

// ---------- 主流程 ----------
const STATE_FILE = path.join(__dirname, 'notion_mcp_state.json');

function loadState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch (e) { return { synced: [] }; }
}
function saveState(s) {
  fs.writeFileSync(STATE_FILE, JSON.stringify(s, null, 2));
}

(async () => {
  const docsDir = process.argv[2] || path.join(__dirname, '../github-repo/docs');
  const parentId = process.argv[3] || '4188dd04-18b0-8147-acac-d9359cf63e67';
  const limitIdx = process.argv.indexOf('--limit');
  const limit = limitIdx > 0 ? parseInt(process.argv[limitIdx + 1], 10) : null;
  const skipExisting = process.argv.includes('--skip-existing');

  await init();
  console.log('[sync] MCP 已连接');

  const files = fs.readdirSync(docsDir).filter(f => f.endsWith('.md'))
    .map(f => ({ file: f, date: (f.match(/^(\d{4}-\d{2}-\d{2})/) || [])[1] }))
    .filter(x => x.date).sort((a, b) => a.date.localeCompare(b.date));

  const state = loadState();
  const synced = new Set(state.synced || []);
  let todo = files.map(x => x.date);
  if (skipExisting) todo = todo.filter(d => !synced.has(d));
  if (limit) todo = todo.slice(-limit);
  console.log('[sync] 总数=%d 待同步=%d', files.length, todo.length);

  const byDate = Object.fromEntries(files.map(x => [x.date, x.file]));
  let ok = 0, fail = 0;
  const BATCH = 5;

  for (let i = 0; i < todo.length; i += BATCH) {
    const batch = todo.slice(i, i + BATCH);
    const pages = batch.map(date => {
      let md = fs.readFileSync(path.join(docsDir, byDate[date]), 'utf8');
      md = md.replace(/^#\s+.*\n/, '');   // 去掉 H1（页面标题已表达）
      return {
        properties: { title: `${date} 新闻联播` },
        icon: '📰',
        content: md,
      };
    });
    const r = await callTool('notion-create-pages', {
      parent: { page_id: parentId },
      allow_async: false,
      pages,
    });
    if (r && Array.isArray(r.pages)) {
      for (const d of batch) { synced.add(d); ok++; }
      state.synced = [...synced].sort();
      state.last_sync = new Date().toISOString();
      saveState(state);
      console.log('[sync] %d/%d 成功 (%s ~ %s)', i + batch.length, todo.length, batch[0], batch[batch.length - 1]);
    } else {
      fail += batch.length;
      console.log('[sync] %d/%d 失败:', i + batch.length, todo.length, JSON.stringify(r).slice(0, 300));
      // 逐个重试
      for (const date of batch) {
        let md = fs.readFileSync(path.join(docsDir, byDate[date]), 'utf8').replace(/^#\s+.*\n/, '');
        const r1 = await callTool('notion-create-pages', {
          parent: { page_id: parentId }, allow_async: false,
          pages: [{ properties: { title: `${date} 新闻联播` }, icon: '📰', content: md }],
        });
        if (r1 && Array.isArray(r1.pages)) { synced.add(date); ok++; state.synced = [...synced].sort(); saveState(state); }
        else { fail++; console.log('[sync] 单页失败 %s:', date, JSON.stringify(r1).slice(0, 200)); }
        await new Promise(r => setTimeout(r, 800));
      }
    }
    await new Promise(r => setTimeout(r, 1200));
  }
  console.log('[sync] 完成：成功=%d 失败=%d', ok, fail);
})();
