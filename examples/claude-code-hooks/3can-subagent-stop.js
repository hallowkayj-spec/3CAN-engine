/**
 * 3CAN SubagentStop Observer — 基座#33
 *
 * Claude Code 的 sub-agent (Task tool 启的) 完成时触发.
 * 抓捕 subagent 产出 (最终 response + tool usage) → activity_log + 可选建 SES-subagent-* 节点
 *
 * 目的: sub-agent 目前是黑盒, 3CAN 不知情. 这个 hook 补闭环.
 *
 * 策略 (不 block, 纯 observe):
 *   - 读 stdin 的 input (含 subagent 最终文本)
 *   - POST /api/route/feedback (借用现有端点记 activity)
 *   - 不建节点 (避免污染, subagent 结果可能很短没价值)
 *
 * 硬超时 2s, 失败静默.
 */

const http = require('http');

const ENGINE_URL = 'http://localhost:9700';
const TIMEOUT = 2000;
const AGENT_ID = process.env.CLAUDE_AGENT_ID || 'opus-brain-main';

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
    setTimeout(() => resolve(data), 400);
  });
}

async function main() {
  const raw = await readStdin();
  let input;
  try { input = JSON.parse(raw); } catch { process.exit(0); }

  const subagentType = input.subagent_type || input.agent_type || 'unknown';
  const response = input.response || input.subagent_response || '';
  const summary = typeof response === 'string' ? response.slice(0, 300) : JSON.stringify(response).slice(0, 300);

  await httpPost('/api/route/feedback', {
    query: `[subagent-stop] type=${subagentType}`,
    agent_id: AGENT_ID,
    details: summary,
    affected_nodes: [],
  });

  process.exit(0);
}

main().catch(() => process.exit(0));
