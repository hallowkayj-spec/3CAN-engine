/**
 * 3CAN SessionEnd Summary — 基座#34
 *
 * Session 真结束 (用户退出 Claude Code, 或超时) 触发.
 * 和 Stop 不同, SessionEnd 是**最终**的, Stop 可能后续 compact 继续.
 *
 * 策略:
 *   - 读 activity_log 最近 N 条属于本 session
 *   - 生成一个 SES-session-end-{timestamp} 节点
 *   - 写入 3CAN (避免 session 总结丢失)
 *
 * 硬超时 8s. 失败静默.
 */

const http = require('http');

const ENGINE_URL = 'http://localhost:9700';
const TIMEOUT = 3000;
const AGENT_ID = process.env.CLAUDE_AGENT_ID || 'opus-brain-main';
const LOOKBACK_N = 100;

function httpGet(pathStr) {
  return new Promise((resolve) => {
    const url = new URL(pathStr, ENGINE_URL);
    const req = http.request({
      hostname: url.hostname, port: url.port, path: url.pathname + url.search,
      method: 'GET', timeout: TIMEOUT,
    }, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => { try { resolve({ ok: true, data: JSON.parse(data) }); } catch { resolve({ ok: false }); } });
    });
    req.on('error', () => resolve({ ok: false }));
    req.on('timeout', () => { req.destroy(); resolve({ ok: false }); });
    req.end();
  });
}

function httpPost(pathStr, body) {
  return new Promise((resolve) => {
    const url = new URL(pathStr, ENGINE_URL);
    const payload = JSON.stringify(body || {});
    const req = http.request({
      hostname: url.hostname, port: url.port, path: url.pathname + url.search,
      method: 'POST', timeout: TIMEOUT,
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(payload) },
    }, (res) => {
      let data = '';
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve({ status: res.statusCode }));
    });
    req.on('error', () => resolve({ status: 0 }));
    req.on('timeout', () => { req.destroy(); resolve({ status: 0 }); });
    req.write(payload); req.end();
  });
}

async function readStdin() {
  return new Promise((resolve) => {
    let data = '';
    process.stdin.setEncoding('utf-8');
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
    setTimeout(() => resolve(data), 500);
  });
}

async function main() {
  try { await readStdin(); } catch { }

  // 拉最近 100 条活动
  const actR = await httpGet(`/api/activity?limit=${LOOKBACK_N}`);
  if (!actR.ok) { process.exit(0); return; }

  const acts = actR.data || [];
  // 只保留本 agent 的活动 (粗滤)
  const mine = acts.filter((a) => (a.agent_id || '') === AGENT_ID);
  if (mine.length < 3) { process.exit(0); return; }  // 没够数据就不写

  const startTs = mine[0].timestamp || '';
  const endTs = mine[mine.length - 1].timestamp || new Date().toISOString();
  const actionCounts = {};
  const affectedNodes = new Set();
  for (const a of mine) {
    actionCounts[a.action || 'unknown'] = (actionCounts[a.action || 'unknown'] || 0) + 1;
    for (const n of (a.affected_nodes || [])) affectedNodes.add(n);
  }

  const ymd = new Date().toISOString().slice(0, 10).replace(/-/g, '');
  const hm = new Date().toISOString().slice(11, 16).replace(':', '');
  const nodeId = `SES-session-end-${AGENT_ID.replace(/[^a-zA-Z0-9]/g, '-').slice(0, 20)}-${ymd}T${hm}`;

  const actionsStr = Object.entries(actionCounts)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}=${v}`)
    .join(', ');

  const affectedTop = Array.from(affectedNodes).slice(0, 20);

  const node = {
    id: nodeId.slice(0, 60),
    name: `[auto-ses-end] ${AGENT_ID} ${startTs.slice(0, 16)} - ${endTs.slice(11, 16)} (${mine.length} 事件)`.slice(0, 80),
    cluster: '会话记录',
    type: 'session',
    status: 'active',
    priority: 'medium',
    content: {
      description: `SessionEnd 自动 summary: ${AGENT_ID} 于 ${startTs.slice(0, 19)} 至 ${endTs.slice(0, 19)} 共 ${mine.length} 个事件. Actions: ${actionsStr}.`,
      current_state: 'session-ended, auto-summary written',
      notes: `Top actions: ${actionsStr}\nAffected nodes top 20: ${affectedTop.join(', ')}\nStart: ${startTs}\nEnd: ${endTs}`,
      extra: {
        aggregated_agent: AGENT_ID,
        start: startTs,
        end: endTs,
        n_events: mine.length,
        action_counts: actionCounts,
      },
    },
    activation_keywords: [
      AGENT_ID, ymd, 'session-end', 'auto-summary',
      ...Object.keys(actionCounts).slice(0, 5),
    ],
    primary_author: 'session-end-hook',
  };

  await httpPost('/api/nodes?force=true', node);
  process.exit(0);
}

main().catch(() => process.exit(0));
