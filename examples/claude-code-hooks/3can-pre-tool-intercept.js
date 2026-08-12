/**
 * 3CAN PreToolUse Intercept — 平台级认知harness
 *
 * 当agent想grep memory/或读handoff文件时，自动先查3CAN，
 * 把结果注入context。agent不需要"记住要用3CAN"——hook替他做了。
 *
 * 拦截范围:
 *   Grep memory/ → 自动route 3CAN
 *   Read memory/*.md → 自动route 3CAN
 *   Read handoffs/active/*.md → 提示3CAN已有索引
 *
 * 不阻断: agent仍可继续原操作，但3CAN结果已先到。
 */

const http = require('http');

const ENGINE_URL = 'http://localhost:9700';
const TIMEOUT = 4000;

function httpPost(path, body) {
  return new Promise((resolve) => {
    const url = new URL(path, ENGINE_URL);
    const data = JSON.stringify(body);
    const options = {
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
      timeout: TIMEOUT,
    };
    const req = http.request(options, (res) => {
      let chunks = '';
      res.on('data', (c) => { chunks += c; });
      res.on('end', () => {
        try { resolve(JSON.parse(chunks)); }
        catch { resolve(null); }
      });
    });
    req.on('error', () => resolve(null));
    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.write(data);
    req.end();
  });
}

async function main() {
  // 读取stdin获取hook input
  let input = '';
  for await (const chunk of process.stdin) {
    input += chunk;
  }

  let hookInput;
  try {
    hookInput = JSON.parse(input);
  } catch {
    process.exit(0);
  }

  const toolName = hookInput.toolName || '';
  const toolInput = hookInput.toolInput || {};

  // 判断是否在访问memory/handoff相关路径
  const targetPath = toolInput.path || toolInput.file_path || toolInput.pattern || toolInput.command || '';
  const isMemoryAccess = /memory[\/\\]|MEMORY\.md/i.test(targetPath);
  const isHandoffAccess = /handoff/i.test(targetPath);

  if (!isMemoryAccess && !isHandoffAccess) {
    // 不是记忆相关操作，放行
    process.exit(0);
  }

  // 提取查询关键词
  let searchTerms = '';
  if (toolName === 'Grep') {
    searchTerms = toolInput.pattern || '';
  } else if (toolName === 'Read') {
    // 从文件名提取关键词
    const fname = (targetPath.split('/').pop() || targetPath.split('\\').pop() || '').replace(/[-_\.]/g, ' ').replace(/\.md$/, '');
    searchTerms = fname;
  } else if (toolName === 'Bash') {
    // 从grep命令提取pattern
    const grepMatch = targetPath.match(/grep\s+(?:-[a-zA-Z]+\s+)*["']?([^"'\s|]+)/);
    if (grepMatch) searchTerms = grepMatch[1];
  }

  if (!searchTerms) {
    process.exit(0);
  }

  // 自动查3CAN
  const result = await httpPost('/api/route', {
    task: searchTerms,
    max_nodes: 4,
    agent_id: 'harness-intercept',
  });

  if (!result || !result.activated_nodes || result.activated_nodes.length === 0) {
    process.exit(0);
  }

  // 构建提示
  const nodes = result.activated_nodes;
  const lines = [
    `[3CAN Harness] 检测到你在查 ${isMemoryAccess ? 'memory' : 'handoff'} 文件。3CAN已有相关节点:`,
  ];
  for (const n of nodes.slice(0, 4)) {
    const state = (n.content && n.content.current_state) ? ` — ${n.content.current_state.substring(0, 50)}` : '';
    lines.push(`  ${n.id}: ${(n.name || '').substring(0, 45)}${state}`);
  }
  lines.push('可直接用3CAN route结果，无需读原始文件。如需原文细节可继续读。');

  console.log(JSON.stringify({
    hookSpecificOutput: {
      additionalContext: lines.join('\n'),
    },
  }));
  process.exit(0);
}

main().catch(() => process.exit(0));
