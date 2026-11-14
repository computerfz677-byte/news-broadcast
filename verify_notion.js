const { init, callTool } = require('./notion_mcp_client.js');

const PARENT = '4188dd04-18b0-8147-acac-d9359cf63e67';

(async () => {
  await init();
  const r = await callTool('notion-fetch', { id: PARENT });
  const inner = (r && r.text) || String(r);
  const items = [...inner.matchAll(/<page url="([^"]+)">\s*([^\n<]+)\s*<\/page>/g)]
    .map((x) => ({ id: x[1], title: x[2].trim() }));
  const byTitle = {};
  for (const it of items) {
    (byTitle[it.title] = byTitle[it.title] || []).push(it.id);
  }
  const dups = Object.entries(byTitle).filter(([, v]) => v.length > 1);
  console.log('子页条目数:', items.length);
  console.log('唯一标题数:', Object.keys(byTitle).length);
  console.log('重复标题:', dups.map(([t, v]) => `${t} (${v.length}次)`).join(' | ') || '无');
  const dates = items.map((x) => x.title).filter((t) => /^\d{4}-\d{2}-\d{2}/.test(t)).map((t) => t.slice(0, 10)).sort();
  console.log('简报页数:', dates.length, '| 最早:', dates[0], '| 最新:', dates[dates.length - 1]);
  // 缺期检测
  const fs = require('fs');
  const local = fs.readdirSync('C:/Users/CFZ/WorkBuddy/news-broadcast/github-repo/docs')
    .filter((f) => f.endsWith('.md'))
    .map((f) => f.slice(0, 10)).sort();
  const missing = local.filter((d) => !dates.includes(d));
  console.log('本地期数:', local.length, '| Notion 缺失:', missing.length ? missing.join(',') : '无');
  if (process.argv[2] === '--delete-dups') {
    for (const [title, ids] of dups) {
      for (const id of ids.slice(1)) {  // 保留第一个
        const del = await callTool('notion-delete-page', { page_id: id });
        console.log('删除重复', title, id, '->', JSON.stringify(del).slice(0, 150));
      }
    }
  }
})();
